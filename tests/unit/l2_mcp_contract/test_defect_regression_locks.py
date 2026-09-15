"""Regression locks for the F-01/F-02 fixes (2026-09-08).

Both defects were found by the v4 real-wire verification round and
locked as xfail(strict) in docs/archive/specs/design_ppsspp_dfx_mcp_test_module_refactor_v1
R-D. This commit fixes them and flips the markers per the plan:

- F-01 (P1): ppsspp_breakpoint(action=mem_set/mem_remove/mem_update)
  must reject size < 1 — a zero-width watchpoint is stored by PPSSPP
  but can never hit, and it stacks invisibly with same-address
  memchecks. Real-wire evidence: v4 probe P1.bp.mem_set_sizes.
- F-02 (P2): ppsspp_replay(action=save) must reject an empty capture
  (size=0) with [REPLAY_EMPTY] instead of writing a corrupt 0-byte
  .ppr and reporting ok. Real-wire evidence: v4 round-1
  P1.rp.roundtrip (begin → immediate save).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import ppsspp_dfx_mcp.tools.breakpoint as bp_mod
import ppsspp_dfx_mcp.tools.replay as replay_mod
from ppsspp_dfx_mcp.errors import ToolError

SCRATCH = "0x09FE0000"


class _RecordingClient:
    """Records mem_bp_add calls; behaves like PPSSPP (stores anything)."""

    def __init__(self) -> None:
        self.mem_adds: list[dict] = []

    async def mem_bp_add(self, address, size, read=True, write=True,
                         enabled=True, log=False, **kw):
        self.mem_adds.append({"address": address, "size": size,
                              "read": read, "write": write})
        return {}

    async def cpu_bp_list(self):
        return {"breakpoints": []}

    async def mem_bp_list(self):
        return {"breakpoints": [
            {"address": a["address"], "size": a["size"], "hits": 0}
            for a in self.mem_adds
        ]}


class _ReplayClient:
    """F-02 stub: flush returns an EMPTY capture (size=0 base64)."""

    def __init__(self) -> None:
        self.flushes = 0

    async def replay_flush(self):
        self.flushes += 1
        return {"version": 1, "base64": ""}

    async def replay_time_get(self):
        return {"value": 12345}


@pytest.mark.asyncio
async def test_mem_set_rejects_zero_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """F-01 fixed: mem_set(size=0) is rejected before any PPSSPP call."""
    client = _RecordingClient()

    @asynccontextmanager
    async def fake_sc(session_id: str):
        yield client

    monkeypatch.setattr(bp_mod, "session_client", fake_sc)
    with pytest.raises(ToolError, match="invalid memcheck size 0"):
        await bp_mod.breakpoint(
            session_id="s1", action="mem_set", address=SCRATCH, size=0,
        )
    assert client.mem_adds == [], "reject must happen BEFORE the mem_bp_add"


@pytest.mark.asyncio
async def test_mem_remove_rejects_negative_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _RecordingClient()

    @asynccontextmanager
    async def fake_sc(session_id: str):
        yield client

    monkeypatch.setattr(bp_mod, "session_client", fake_sc)
    with pytest.raises(ToolError, match="invalid memcheck size -4"):
        await bp_mod.breakpoint(
            session_id="s1", action="mem_remove", address=SCRATCH,
            size=-4,
        )


@pytest.mark.asyncio
async def test_mem_set_accepts_range_watch_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive sizes beyond {1,2,4} stay legal (v4 probe armed a
    size=64 range watch successfully) — only < 1 is invalid."""
    client = _RecordingClient()

    @asynccontextmanager
    async def fake_sc(session_id: str):
        yield client

    monkeypatch.setattr(bp_mod, "session_client", fake_sc)
    result = await bp_mod.breakpoint(
        session_id="s1", action="mem_set", address=SCRATCH, size=64,
    )
    assert [a["size"] for a in client.mem_adds] == [64]
    assert result["action"] == "mem_set"


@pytest.mark.asyncio
async def test_replay_save_rejects_empty_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """F-02 fixed: an empty capture is rejected with [REPLAY_EMPTY]
    BEFORE any file is written."""
    client = _ReplayClient()

    @asynccontextmanager
    async def fake_sc(session_id: str):
        yield client

    def fake_resolve(subdir: str, filename: str) -> Path:
        return tmp_path / subdir / filename

    monkeypatch.setattr(replay_mod, "session_client", fake_sc)
    monkeypatch.setattr(replay_mod, "resolve_output_path", fake_resolve)
    with pytest.raises(ToolError, match="no frames captured") as info:
        await replay_mod.replay(
            session_id="s1", action="save", file_path="roundtrip.ppr",
        )
    assert "[REPLAY_EMPTY]" in str(info.value)
    assert client.flushes == 1
    assert not list(tmp_path.rglob("*.ppr")), "no .ppr may be written"
