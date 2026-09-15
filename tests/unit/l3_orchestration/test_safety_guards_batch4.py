"""L3 orchestration tests: safety guards (batch 4).

Anchors:
- D-19: disassemble count<=0 returns empty without calling PPSSPP
- D-05: write_memory protected address check + force override
- D-21: mem_update merges read/write/change from current state
- D-31: session start port conflict detection

L3 focus (tool wrapper orchestration, NOT WS forwarding):
- disassemble: count=0 returns empty instructions, client.disasm NOT called
- write_memory: address in kernel/code range raises ToolError; force=True bypasses
- breakpoint(mem_update): partial read/write triggers mem_bp_list query + merge
- session_manager: _check_port_conflict raises PortConflict on duplicate port
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest

from ppsspp_dfx_mcp.errors import PortConflict, ToolError
from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session.session_manager import (
    _check_port_conflict,
    _extract_port_from_ws_url,
)
from ppsspp_dfx_mcp.tools.assemble import assemble
from ppsspp_dfx_mcp.tools.breakpoint import breakpoint
from ppsspp_dfx_mcp.tools.memory import disassemble, read_memory, write_memory


# ============================================================================
# D-19: disassemble count<=0 returns empty
# ============================================================================


class TestDisassembleCountZero:
    """L3: disassemble(count=0) returns empty result without calling PPSSPP."""

    @pytest.mark.asyncio
    async def test_count_zero_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """count=0 → empty instructions, client.disasm NOT called."""
        mock_client = AsyncMock()

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        result = await disassemble(
            session_id="sess-1", address=0x08804000, count=0
        )

        assert result["count"] == 0
        assert result["instructions"] == []
        assert not mock_client.disasm.await_count, (
            "D-19: count=0 must NOT call client.disasm — PPSSPP returns "
            "'Missing end parameter' error for count=0."
        )

    @pytest.mark.asyncio
    async def test_count_negative_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """count=-1 → empty result, client.disasm NOT called."""
        mock_client = AsyncMock()

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        result = await disassemble(
            session_id="sess-1", address=0x08804000, count=-1
        )

        assert result["count"] == 0
        assert result["instructions"] == []
        mock_client.disasm.assert_not_awaited()


# ============================================================================
# D-05: write_memory protected address check
# ============================================================================


class TestWriteMemoryProtectedAddress:
    """L3: write_memory rejects protected addresses unless force=True."""

    @pytest.mark.asyncio
    async def test_kernel_address_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Writing to kernel memory (< 0x08800000) raises ToolError."""
        mock_client = AsyncMock()

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        with pytest.raises(ToolError) as exc_info:
            await write_memory(
                session_id="sess-1",
                address=0x00010000,
                data=0xDEADBEEF,
                format="u32",
            )

        assert exc_info.value.code == "PROTECTED_ADDRESS"
        mock_client.write_u32.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_code_section_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Writing to top.prx code section raises ToolError."""
        mock_client = AsyncMock()

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        with pytest.raises(ToolError):
            await write_memory(
                session_id="sess-1",
                address=0x08804000,
                data=0x00000000,
                format="u32",
            )

        mock_client.write_u32.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_overrides_protection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force=True allows writing to protected address."""
        mock_client = AsyncMock()

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        result = await write_memory(
            session_id="sess-1",
            address=0x08804000,
            data=0x00000000,
            format="u32",
            force=True,
        )

        mock_client.write_u32.assert_awaited_once_with(
            address=0x08804000, value=0x00000000
        )
        assert result["bytes_written"] == 4

    @pytest.mark.asyncio
    async def test_safe_address_not_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Writing to data section (0x09000000) works without force."""
        mock_client = AsyncMock()

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        result = await write_memory(
            session_id="sess-1",
            address=0x09000000,
            data=0x12345678,
            format="u32",
        )

        mock_client.write_u32.assert_awaited_once()
        assert result["bytes_written"] == 4


# ============================================================================
# N-04: assemble protected address check (mirrors D-05 write_memory)
# ============================================================================


class TestAssembleProtectedAddress:
    """L3: assemble rejects protected addresses unless force=True (N-04).

    Pre-N-04, assemble called ``client.assemble`` directly with no
    protected-address check, allowing accidental writes to kernel memory
    or top.prx code section that crash PPSSPP via JIT cache
    invalidation. The fix reuses the shared ``check_protected_address``
    helper from ``service.memory_protection`` so assemble and
    write_memory enforce the same protection policy.
    """

    @pytest.mark.asyncio
    async def test_kernel_address_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assembling to kernel memory (< 0x08800000) raises ToolError."""
        mock_client = AsyncMock()

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.assemble.session_client",
            fake_session_client,
        )

        with pytest.raises(ToolError) as exc_info:
            await assemble(
                session_id="sess-1",
                address=0x00010000,
                code="nop",
            )

        assert exc_info.value.code == "PROTECTED_ADDRESS"
        mock_client.assemble.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_code_section_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assembling to top.prx code section raises ToolError."""
        mock_client = AsyncMock()

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.assemble.session_client",
            fake_session_client,
        )

        with pytest.raises(ToolError):
            await assemble(
                session_id="sess-1",
                address=0x08804000,
                code="nop",
            )

        mock_client.assemble.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_multi_instruction_range_rejected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multi-instruction range straddling protected boundary rejected.

        The full write range [address, address + len(instructions) * 4)
        is checked via overlap test (``address < hi and end > lo``).
        With 3 instructions starting at 0x08803FF8 the range spans
        [0x08803FF8, 0x08804004) which crosses into the top.prx code
        section at 0x08804000 — must be rejected even though the start
        address itself is in the safe gap.
        """
        mock_client = AsyncMock()

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.assemble.session_client",
            fake_session_client,
        )

        # 3 instructions × 4 bytes = 12 bytes; range [0x08803FF8, 0x08804004)
        # overlaps top.prx code section [0x08804000, 0x08D34000).
        with pytest.raises(ToolError):
            await assemble(
                session_id="sess-1",
                address=0x08803FF8,
                code="nop\nnop\nnop",
            )

        mock_client.assemble.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_overrides_protection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force=True allows assembling into protected address."""
        mock_client = AsyncMock()
        mock_client.assemble.return_value = {"encoding": 0x00000000}

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.assemble.session_client",
            fake_session_client,
        )

        result = await assemble(
            session_id="sess-1",
            address=0x08804000,
            code="nop",
            force=True,
        )

        mock_client.assemble.assert_awaited_once_with(
            address=0x08804000, code="nop"
        )
        assert result["bytes_written"] == 4

    @pytest.mark.asyncio
    async def test_safe_address_not_blocked(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assembling to data section (0x09000000) works without force."""
        mock_client = AsyncMock()
        mock_client.assemble.return_value = {"encoding": 0x00000000}

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.assemble.session_client",
            fake_session_client,
        )

        result = await assemble(
            session_id="sess-1",
            address=0x09000000,
            code="nop",
        )

        mock_client.assemble.assert_awaited_once()
        assert result["bytes_written"] == 4


# ============================================================================
# D-21: mem_update merges read/write/change
# ============================================================================


class TestMemUpdateParamMerge:
    """L3: mem_update merges un-passed read/write from current state (D-21).

    PPSSPP's WebSocketMemoryBreakpointUpdate defaults read/write/change
    to false when not provided. The tool queries the current breakpoint
    state and merges un-passed params so PPSSPP receives the complete
    triple.
    """

    @pytest.mark.asyncio
    async def test_partial_read_update_queries_current_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """mem_update(read=False) triggers mem_bp_list to fetch current write."""
        mock_client = AsyncMock()
        # Current state: read=True, write=True, change=False.
        mock_client.mem_bp_list.return_value = {
            "breakpoints": [
                {"address": 0x08810000, "size": 4, "read": True, "write": True, "change": False}
            ]
        }
        mock_client.mem_bp_update.return_value = {}

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.breakpoint.session_client",
            fake_session_client,
        )

        await breakpoint(
            session_id="sess-1",
            action="mem_update",
            address=0x08810000,
            size=4,
            read=False,
        )

        # Verify mem_bp_list was called for current state.
        assert mock_client.mem_bp_list.await_count >= 1

        # Verify mem_bp_update received merged params:
        # read=False (from caller), write=True (from current state),
        # change=False (from current state).
        call_kwargs = mock_client.mem_bp_update.call_args.kwargs
        assert call_kwargs["read"] is False, (
            "D-21: read=False from caller must be forwarded."
        )
        assert call_kwargs["write"] is True, (
            "D-21: write must be merged from current state (True), not "
            "silently reset to False by PPSSPP's default."
        )

    @pytest.mark.asyncio
    async def test_enabled_only_update_still_merges_read_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """S4 gap (a) regression: mem_update(enabled=False) must STILL
        merge read/write/change from current state.

        The pre-S4 logic only merged when read/write were passed, so an
        enabled-only update omitted all three flags and PPSSPP silently
        reset the watchpoint's read/write to false.
        """
        mock_client = AsyncMock()
        mock_client.mem_bp_list.return_value = {
            "breakpoints": [
                {"address": 0x08810000, "size": 4, "read": True, "write": True, "change": False}
            ]
        }
        mock_client.mem_bp_update.return_value = {}

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.breakpoint.session_client",
            fake_session_client,
        )

        await breakpoint(
            session_id="sess-1",
            action="mem_update",
            address=0x08810000,
            size=4,
            enabled=False,
        )

        # S4: unconditional merge → mem_bp_list called twice (merge query
        # + follow-up list), and the update carries the complete triple.
        assert mock_client.mem_bp_list.await_count == 2
        call_kwargs = mock_client.mem_bp_update.call_args.kwargs
        assert call_kwargs["read"] is True, (
            "S4: enabled-only update must preserve read=True from current state."
        )
        assert call_kwargs["write"] is True, (
            "S4: enabled-only update must preserve write=True from current state."
        )
        assert call_kwargs["change"] is False
        assert call_kwargs["enabled"] is False

    @pytest.mark.asyncio
    async def test_merge_matches_by_address_not_caller_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """S4 gap (b) regression: the memcheck is matched by ADDRESS only.

        A 16-byte watch updated with the caller's default size=4 used to
        miss the merge lookup (exact size match), silently resetting
        read/write. The update must carry the memcheck's ACTUAL size.
        """
        mock_client = AsyncMock()
        mock_client.mem_bp_list.return_value = {
            "breakpoints": [
                {"address": 0x08810000, "size": 16, "read": False, "write": True, "change": True}
            ]
        }
        mock_client.mem_bp_update.return_value = {}

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.breakpoint.session_client",
            fake_session_client,
        )

        await breakpoint(
            session_id="sess-1",
            action="mem_update",
            address=0x08810000,
            size=4,  # caller's default — must NOT be used for matching
            read=True,
        )

        call_kwargs = mock_client.mem_bp_update.call_args.kwargs
        assert call_kwargs["size"] == 16, (
            "S4: update must forward the memcheck's actual size, not the "
            "caller's (possibly default) size."
        )
        assert call_kwargs["read"] is True  # caller override
        assert call_kwargs["write"] is True  # merged from current
        assert call_kwargs["change"] is True  # merged from current


# ============================================================================
# D-31: session start port conflict
# ============================================================================


class TestPortConflictCheck:
    """L3: _check_port_conflict raises PortConflict on duplicate port."""

    def test_conflict_detected(self) -> None:
        """Two active sessions on same port → PortConflict."""
        sessions = {
            "sess-1": Session(
                session_id="sess-1",
                iso_path="/test.iso",
                pid=12345,
                ws_url="ws://127.0.0.1:12345/debugger",
            ),
        }
        # Stub is_pid_alive so the fake PID is treated as live.
        with patch(
            "ppsspp_dfx_mcp.core.proc.is_pid_alive",
            return_value=True,
        ):
            with pytest.raises(PortConflict) as exc_info:
                _check_port_conflict(sessions, "sess-2", 12345)

        assert "12345" in str(exc_info.value)
        assert "sess-1" in str(exc_info.value)

    def test_no_conflict_different_port(self) -> None:
        """Different ports → no conflict."""
        sessions = {
            "sess-1": Session(
                session_id="sess-1",
                iso_path="/test.iso",
                pid=12345,
                ws_url="ws://127.0.0.1:12345/debugger",
            ),
        }
        # Should not raise.
        assert _check_port_conflict(sessions, "sess-2", 12346) is None

    def test_stopped_session_no_conflict(self) -> None:
        """Stopped session (pid=None) → port is free."""
        sessions = {
            "sess-1": Session(
                session_id="sess-1",
                iso_path="/test.iso",
                pid=None,  # Stopped.
                ws_url="ws://127.0.0.1:12345/debugger",
            ),
        }
        # Should not raise — stopped session's port is free.
        assert _check_port_conflict(sessions, "sess-2", 12345) is None

    def test_same_session_id_no_conflict(self) -> None:
        """Same session_id → no self-conflict."""
        sessions = {
            "sess-1": Session(
                session_id="sess-1",
                iso_path="/test.iso",
                pid=12345,
                ws_url="ws://127.0.0.1:12345/debugger",
            ),
        }
        # Should not raise — same session_id is not a conflict.
        assert _check_port_conflict(sessions, "sess-1", 12345) is None


class TestExtractPortFromWsUrl:
    """L3: _extract_port_from_ws_url parses port correctly."""

    def test_standard_url(self) -> None:
        assert _extract_port_from_ws_url("ws://127.0.0.1:12345/debugger") == 12345

    def test_no_path(self) -> None:
        assert _extract_port_from_ws_url("ws://localhost:8080") == 8080

    def test_malformed_url(self) -> None:
        assert _extract_port_from_ws_url("not-a-url") is None

    def test_no_port(self) -> None:
        assert _extract_port_from_ws_url("ws://localhost/debugger") is None


# ============================================================================
# W4: scan chunk clamp + range cap; 建议6: empty bytes write rejected
# ============================================================================


class TestW4ScanClamp:
    """W4 fix: scan chunk_size is clamped to [64, 65536] so it can never
    bypass the 64 KiB single-read cap, and oversized ranges are rejected."""

    @pytest.mark.asyncio
    async def test_huge_chunk_clamped_to_64k(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client = AsyncMock()
        mock_client.scan_memory.return_value = []

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        await read_memory(
            session_id="sess-1",
            action="scan",
            pattern="AABB",
            start_addr="0x08800000",
            end_addr="0x08810000",
            chunk_size=512 * 1024 * 1024,
        )
        kwargs = mock_client.scan_memory.call_args.kwargs
        assert kwargs["chunk_size"] == 65536

    @pytest.mark.asyncio
    async def test_tiny_chunk_clamped_to_64(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client = AsyncMock()
        mock_client.scan_memory.return_value = []

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        await read_memory(
            session_id="sess-1",
            action="scan",
            pattern="AABB",
            start_addr="0x08800000",
            end_addr="0x08810000",
            chunk_size=1,
        )
        kwargs = mock_client.scan_memory.call_args.kwargs
        assert kwargs["chunk_size"] == 64

    @pytest.mark.asyncio
    async def test_oversized_range_rejected(self) -> None:
        # 0x08800000 → 0x88800000 = 2 GiB, well past the 256 MiB cap.
        with pytest.raises(ToolError, match="scan range too large"):
            await read_memory(
                session_id="sess-1",
                action="scan",
                pattern="AABB",
                start_addr="0x08800000",
                end_addr="0x88800000",
            )

    @pytest.mark.asyncio
    async def test_normal_range_still_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client = AsyncMock()
        mock_client.scan_memory.return_value = []

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )
        # 64 MiB (full PSP RAM view) must remain scannable.
        await read_memory(
            session_id="sess-1",
            action="scan",
            pattern="AABB",
            start_addr="0x08000000",
            end_addr="0x0C000000",
        )
        mock_client.scan_memory.assert_awaited_once()


class TestEmptyBytesWriteRejected:
    """建议6 fix: format='bytes' with data decoding to zero bytes is an
    error, not a silent no-op success."""

    @pytest.mark.asyncio
    async def test_empty_hex_writes_nothing(self) -> None:
        with pytest.raises(ToolError, match="zero bytes"):
            await write_memory(
                session_id="sess-1",
                address="0x09FE0000",
                data="",
                format="bytes",
            )
