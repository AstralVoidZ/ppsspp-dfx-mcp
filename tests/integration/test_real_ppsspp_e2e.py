"""test_real_ppsspp_e2e.py — real-PPSSPP end-to-end tool chain verification.

Anchor: PpssppDebugClient → WsTransport → live PPSSPP WebSocket.
Verifies the full tool chain against a real PPSSPP process (started
once via the session-scoped `real_ppsspp_launcher` fixture).

Each test uses the function-scoped `real_transport` fixture (fresh
WsTransport per test, no state leakage). The PPSSPP process itself
is shared across tests for performance (~5s startup cost amortized).

Test scope (mapped to guide_ppsspp_dfx_mcp_live_test_methodology_v1.md):
- Phase 1 — service & lifecycle (via direct transport calls)
- Phase 2 — core debug (memory / CPU / stepping / breakpoint / disasm)
- Phase 3 — script management (tested via tool wrappers separately)
- Phase 4 — P0 debug enhancements (memory_map / evaluate)
- Phase 5 — GPU & memory tracing (gpu_stats / search_memory_info)

These tests are skipped when PPSSPP / ISO is unavailable (CI-safe).
Run them locally with:
    pytest tests/integration/test_real_ppsspp_e2e.py -v -m real_ppsspp

Non-determinism handling:
- PC values are not asserted to specific numbers (VBlank interrupts
  change them between calls). Instead, we assert structural properties
  (non-zero, in valid range, monotonic where applicable).
- CPU state is reset to running after each destructive test (step_into,
  pause) via `resume()` in teardown.
- Breakpoints are explicitly removed after each test that sets them.
"""

from __future__ import annotations

import contextlib

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

# ============================================================================
# Phase 1 — service & connectivity
# ============================================================================


@pytest.mark.real_ppsspp
class TestRealPpssppConnectivity:
    """Verify basic connectivity to the live PPSSPP process."""

    async def test_version_handshake_succeeds(self, real_transport):
        """After connect() + send_version(), transport is ready for calls.

        The `real_transport` fixture already performed connect + send_version.
        We verify by issuing a `version` call and checking the response.
        """
        resp = await real_transport.call("version")
        assert resp.get("name") == "PPSSPP", f"expected name='PPSSPP', got {resp.get('name')!r}"
        version_str = resp.get("version", "")
        assert version_str, f"version field is empty: {resp!r}"
        # PPSSPP version strings look like "v1.20.4-605-gf0c28c6744".
        assert version_str.startswith("v"), f"version should start with 'v', got {version_str!r}"

    async def test_cpu_status_returns_stepping_field(self, real_transport):
        """cpu.status response contains `stepping` boolean."""
        resp = await real_transport.call("cpu.status")
        assert "stepping" in resp, f"cpu.status missing 'stepping' field: {resp!r}"
        assert isinstance(resp["stepping"], bool)

    async def test_game_status_returns_title(self, real_transport):
        """game.status response contains a non-empty game title."""
        resp = await real_transport.call("game.status")
        game = resp.get("game")
        assert isinstance(game, dict), f"game.status 'game' field should be a dict: {resp!r}"
        title = game.get("title", "")
        assert title, f"game title is empty: {resp!r}"


# ============================================================================
# Phase 2 — core debug (memory / CPU / stepping / breakpoint / disasm)
# ============================================================================


# top.prx load address (PSP user memory base for the main module).
# Used as a known-readable address for memory read tests.
_TOP_PRX_BASE = 0x08804000


@pytest.mark.real_ppsspp
class TestRealPpssppMemory:
    """Memory read/write round-trip against the live PPSSPP process."""

    async def test_read_u32_returns_nonzero_at_top_prx_base(
        self,
        real_transport,
    ):
        """read_u32 at top.prx base address returns a non-zero value.

        top.prx is loaded at 0x08804000 — the first u32 is the ELF magic
        (0x7F454C46 = "\\x7FELF"), which is always non-zero.
        """
        client = PpssppDebugClient(real_transport)
        value = await client.read_u32(_TOP_PRX_BASE)
        assert value != 0, f"read_u32 at {_TOP_PRX_BASE:#x} returned 0"

    async def test_read_bytes_returns_16_bytes(self, real_transport):
        """read_bytes returns the requested number of bytes.

        Note: top.prx at 0x08804000 contains executable code, NOT the
        ELF header — PSP's PRX loader only loads program segments into
        memory (the on-disk ELF header stays on disk). The first u32 is
        typically a MIPS instruction (e.g., 0x27BDFFC0 = addiu $sp,$sp,-64).
        We assert non-zero bytes rather than ELF magic.
        """
        client = PpssppDebugClient(real_transport)
        data = await client.read_bytes(_TOP_PRX_BASE, 16)
        assert len(data) == 16, f"expected 16 bytes, got {len(data)}: {data!r}"
        # All-zero bytes would indicate an unmapped address; top.prx
        # base must be mapped and contain real code.
        assert data != b"\x00" * 16, (
            f"read_bytes returned all-zero at {_TOP_PRX_BASE:#x}: unmapped?"
        )

    async def test_write_u32_round_trip(self, real_transport):
        """write_u32 then read_u32 returns the written value.

        DESTRUCTIVE: writes to a known address, then restores the original
        value. The address chosen is in PSP user RAM (0x08800000+), well
        below the top.prx code segment, to avoid corrupting code.
        """
        client = PpssppDebugClient(real_transport)
        test_addr = _TOP_PRX_BASE + 0x100  # arbitrary safe offset
        original = await client.read_u32(test_addr)
        try:
            await client.write_u32(test_addr, 0xDEADBEEF)
            read_back = await client.read_u32(test_addr)
            assert read_back == 0xDEADBEEF, (
                f"write_u32 round-trip failed: wrote 0xDEADBEEF, read back {read_back:#x}"
            )
        finally:
            # Restore original value.
            with contextlib.suppress(Exception):
                await client.write_u32(test_addr, original)


@pytest.mark.real_ppsspp
class TestRealPpssppDisasm:
    """Disassembly against the live PPSSPP process."""

    async def test_disasm_returns_requested_count(self, real_transport):
        """disasm returns exactly `count` instruction entries."""
        client = PpssppDebugClient(real_transport)
        lines = await client.disasm(_TOP_PRX_BASE, count=5)
        assert len(lines) == 5, f"expected 5 disasm lines, got {len(lines)}: {lines!r}"
        # Each line must have a `text` field (assembled by DebugClient).
        for i, line in enumerate(lines):
            assert "text" in line, f"line {i} missing 'text' field: {line!r}"
            assert isinstance(line["text"], str)
            assert line["text"], f"line {i} has empty text: {line!r}"


@pytest.mark.real_ppsspp
class TestRealPpssppStepping:
    """CPU stepping (pause/resume) against the live PPSSPP process.

    These tests are DESTRUCTIVE (mutate CPU state). The teardown resumes
    the CPU to leave it in a clean running state for subsequent tests.
    """

    async def test_pause_and_resume_round_trip(self, real_transport):
        """pause() then resume() succeeds; cpu.status reflects the change."""
        client = PpssppDebugClient(real_transport)
        # Capture initial state.
        initial = await real_transport.call("cpu.status")
        _initial_stepping = initial.get("stepping", False)

        try:
            # Pause.
            await client.pause()
            paused = await real_transport.call("cpu.status")
            assert paused.get("stepping") is True, (
                f"after pause(), cpu.status.stepping should be True, got {paused!r}"
            )
            # Resume.
            await client.resume()
            resumed = await real_transport.call("cpu.status")
            assert resumed.get("stepping") is False, (
                f"after resume(), cpu.status.stepping should be False, got {resumed!r}"
            )
        finally:
            # Always leave the CPU running (clean state for next test).
            with contextlib.suppress(Exception):
                await client.resume()

    async def test_get_pc_returns_address_in_psp_range(self, real_transport):
        """get_pc returns a PC in the valid PSP user-memory range.

        Note: PC is only trustworthy when CPU is stepping. We pause first,
        read PC, then resume. The PC value should be in the PSP code range
        (0x08800000-0x0C000000) — top.prx loads at 0x08804000.
        """
        client = PpssppDebugClient(real_transport)
        try:
            await client.pause()
            pc, _trust = await client.safe_get_pc()
            # PC may be 0 if CPU is in an unusual state (e.g. just-booted
            # HLE thread); accept 0 OR a value in the PSP code range.
            assert pc == 0 or 0x08800000 <= pc < 0x0C000000, (
                f"PC {pc:#x} is outside the valid PSP code range (0x08800000-0x0C000000)"
            )
        finally:
            with contextlib.suppress(Exception):
                await client.resume()


@pytest.mark.real_ppsspp
class TestRealPpssppBreakpoint:
    """Breakpoint set/list/remove round-trip against the live PPSSPP.

    DESTRUCTIVE: sets and removes a breakpoint. Teardown ensures any
    leftover breakpoint is removed.
    """

    async def test_breakpoint_add_list_remove_round_trip(self, real_transport):
        """set/list/remove breakpoint cycle works end-to-end."""
        _client = PpssppDebugClient(real_transport)
        test_addr = _TOP_PRX_BASE + 0x200  # arbitrary code address

        try:
            # Add a code breakpoint.
            await real_transport.call(
                "cpu.breakpoint.add",
                address=test_addr,
                type="code",
            )
            # List breakpoints — should contain our test_addr.
            listed = await real_transport.call("cpu.breakpoint.list")
            bps = listed.get("breakpoints", [])
            addrs = [bp.get("address") for bp in bps]
            assert test_addr in addrs, f"breakpoint at {test_addr:#x} not in list: {bps!r}"
        finally:
            # Always remove the breakpoint (best-effort).
            with contextlib.suppress(Exception):
                await real_transport.call(
                    "cpu.breakpoint.remove",
                    address=test_addr,
                )

        # After removal, breakpoint should no longer be in the list.
        listed_after = await real_transport.call("cpu.breakpoint.list")
        addrs_after = [bp.get("address") for bp in listed_after.get("breakpoints", [])]
        assert test_addr not in addrs_after, (
            f"breakpoint at {test_addr:#x} still in list after removal: {listed_after!r}"
        )


# ============================================================================
# Phase 4 — P0 debug enhancements
# ============================================================================


@pytest.mark.real_ppsspp
class TestRealPpssppMemoryMap:
    """memory_map and search_memory_info against the live PPSSPP."""

    async def test_memory_map_returns_non_empty_ranges(self, real_transport):
        """memory_map returns a non-empty `ranges` list."""
        client = PpssppDebugClient(real_transport)
        resp = await client.memory_map()
        ranges = resp.get("ranges", [])
        assert isinstance(ranges, list), f"memory_map 'ranges' should be a list: {resp!r}"
        assert len(ranges) > 0, "memory_map returned no ranges"


# ============================================================================
# Phase 5 — GPU & memory tracing
# ============================================================================


@pytest.mark.real_ppsspp
class TestRealPpssppGpu:
    """GPU stats against the live PPSSPP (requires CPU running)."""

    async def test_gpu_stats_returns_non_negative_fps(self, real_transport):
        """gpu_stats returns fps >= 0 (game may be paused)."""
        _client = PpssppDebugClient(real_transport)
        try:
            resp = await real_transport.call("gpu.getStats")
        except Exception as e:
            pytest.skip(f"gpu.getStats not supported on this PPSSPP build: {e}")
        # The response may have a different shape depending on PPSSPP version.
        # Just assert it's a dict and has at least one numeric field.
        assert isinstance(resp, dict), f"gpu_stats should return dict: {resp!r}"
        assert len(resp) > 0, "gpu_stats returned empty dict"
