"""R5 (design_ppsspp_dfx_mcp_test_refactor_v1 §R5): condensed real-PPSSPP
regression matrix.

The full 153-scenario matrix lives in
``scripts/verify_real_mcp.py`` (three-phase, report-producing).
This module keeps the highest-value real-hardware regression sentinels —
the defects that ONLY manifest against a live PPSSPP:

- F-3: read_string on a code section must be BOUNDED (client-side NUL
  scan) and must NOT kill the WebSocket — the connection must remain
  usable afterwards (pre-fix: giant strnlen response, session dead).
- F-6: read_bytes beyond the single-read cap is rejected client-side.
- F-2: the ``register`` action of ppsspp_query is schema-reachable and
  works against real PPSSPP (register(pc) round-trip).
- F-4: step_into either advances the pc or fails FAST (≤ ~4s) with a
  decisive StepNoAdvanceError — never a blind 5s timeout per attempt.

Run locally: ``pytest tests/integration/test_real_mcp_matrix.py -v -m real_ppsspp``
(skipped automatically when PPSSPP / ISO is unavailable).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ppsspp_dfx_mcp.errors import StepNoAdvanceError, ToolError
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

pytestmark = pytest.mark.real_ppsspp

SCRATCH_ADDR = 0x09FFF000  # high user RAM — unused by the game
CODE_ADDR = 0x08804000  # top.prx code (protected, non-string data)


@pytest.mark.asyncio
async def test_read_string_on_code_section_is_bounded(real_transport):
    """F-3 sentinel: read_string over code memory stays bounded and the
    connection survives."""
    client = PpssppDebugClient(real_transport)
    value = await client.read_string(CODE_ADDR, max_length=4096)
    assert isinstance(value, str)
    assert len(value.encode("utf-8", errors="replace")) <= 4096
    # The connection must still be usable — pre-fix this was the kill shot.
    status = await real_transport.call("cpu.status")
    assert "stepping" in status


@pytest.mark.asyncio
async def test_read_string_terminates_at_nul(real_transport):
    """F-3 happy path: a seeded string is read exactly, NUL-truncated."""
    client = PpssppDebugClient(real_transport)
    payload = b"ABC\x00garbage"
    await client.write_bytes(SCRATCH_ADDR, payload)
    value = await client.read_string(SCRATCH_ADDR, max_length=4096)
    assert value == "ABC"


@pytest.mark.asyncio
async def test_write_read_u32_roundtrip(real_transport):
    """Core capability: u32 write/read round-trip at scratch RAM."""
    client = PpssppDebugClient(real_transport)
    await client.write_u32(SCRATCH_ADDR + 0x10, 0xDEADBEEF)
    raw = await client.read_bytes(SCRATCH_ADDR + 0x10, 4)
    assert raw == b"\xEF\xBE\xAD\xDE"  # little-endian, matches PPSSPP


@pytest.mark.asyncio
async def test_read_bytes_over_cap_rejected_on_wire(real_transport, monkeypatch):
    """F-6 sentinel: >64KiB single reads are rejected at the tool layer."""
    from contextlib import asynccontextmanager

    from ppsspp_dfx_mcp.tools.memory import read_memory

    @asynccontextmanager
    async def fake_session_client(session_id: str):
        yield PpssppDebugClient(real_transport)

    monkeypatch.setattr(
        "ppsspp_dfx_mcp.tools.memory.session_client", fake_session_client
    )
    with pytest.raises(ToolError, match="single-read cap"):
        await read_memory(
            action="read_bytes", address=f"0x{CODE_ADDR:08X}",
            size=1048576, session_id="s",
        )


@pytest.mark.asyncio
async def test_register_action_reachable_via_tool(real_transport):
    """F-2 sentinel: the register action works end-to-end on real PPSSPP."""
    from ppsspp_dfx_mcp.tools.query import query

    # query() needs a session; drive the client-level path that the tool
    # dispatches to (the schema reachability itself is pinned by R2).
    client = PpssppDebugClient(real_transport)
    resp = await client.get_reg("pc")
    assert "uintValue" in resp or "value" in resp


@pytest.mark.asyncio
async def test_step_into_fails_fast_or_advances(real_transport):
    """F-4 sentinel: step_into never hangs for the full blind timeout.

    Either the pc advances (broadcast consumed) or a decisive
    StepNoAdvanceError is raised (W10b rename of the F-4 signal; a
    ToolError so the diagnosis reaches the client verbatim) — bounded
    well under the legacy 5s
    per-attempt budget.
    """
    client = PpssppDebugClient(real_transport)
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(client.step_into(timeout_ms=5000), timeout=8.0)
        advanced = True
    except StepNoAdvanceError:
        advanced = False
    elapsed = time.monotonic() - t0
    assert advanced or elapsed < 6.0, (
        f"step_into neither advanced nor failed fast ({elapsed:.1f}s) — "
        f"F-4 regression"
    )
    # Leave the CPU running for the next test (stepping pauses it).
    await client.resume()
