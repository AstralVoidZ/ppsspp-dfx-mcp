"""ppsspp_diff_memory contract tests — snapshot/compare/drop/list lifecycle.

Fake-mode: session_client is monkeypatched to yield a scripted fake client
whose read_bytes returns deterministic bytes (first read = base pattern,
later reads = mutated pattern), so the diff lifecycle is exercised
end-to-end without PPSSPP.
"""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp.tools import diff as diff_mod
from ppsspp_dfx_mcp.tools.diff import diff_memory

BASE = bytes((i * 7 + 3) % 256 for i in range(4096))
MUTATED = bytearray(BASE)
MUTATED[100] ^= 0xFF
MUTATED[2000] ^= 0x01
MUTATED[2001] ^= 0x80
MUTATED = bytes(MUTATED)


class FakeClient:
    """Scripted read_bytes: first full-range read returns BASE, then MUTATED."""

    def __init__(self) -> None:
        self.payload = BASE  # 快照读到 BASE；测试在快照后切换 payload 模拟变异

    async def read_bytes(self, address: int, size: int) -> list[int]:
        offset = (address - 0x08804000) % len(self.payload)
        return list(self.payload[offset : offset + size])


class FakeSessionClient:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    async def __aenter__(self) -> FakeClient:
        return self._client

    async def __aexit__(self, *exc) -> None:
        return None


@pytest.fixture()
def fake_diff(monkeypatch: pytest.MonkeyPatch):
    client = FakeClient()
    monkeypatch.setattr(diff_mod, "session_client", lambda session_id: FakeSessionClient(client))
    monkeypatch.setattr(diff_mod, "resolve_session_id", _resolve)
    diff_mod._reset_registry_for_tests()
    return client


async def _resolve(session_id: str | None) -> str:
    return session_id or "sess-fake"


async def test_snapshot_then_compare_reports_changed_bytes(fake_diff):
    snap = await diff_memory(action="snapshot", start="0x08804000", end="0x08805000")
    assert snap["handle"]
    assert snap["size_bytes"] == 4096
    assert len(snap["checksum"]) == 16

    fake_diff.payload = MUTATED  # 快照后内存发生变异
    out = await diff_memory(action="compare", handle=snap["handle"])
    assert out["changed_count"] == 3
    assert out["truncated"] is False
    changed = {c["address"]: (c["old"], c["new"]) for c in out["changes"]}
    assert set(changed) == {"0x08804064", "0x088047D0", "0x088047D1"}
    assert changed["0x08804064"] == (BASE[100], MUTATED[100])
    assert len(out["changes"]) == 3


async def test_compare_identical_range_reports_zero(fake_diff):
    snap = await diff_memory(action="snapshot", start="0x08804000", end="0x08805000")
    fake_diff.payload = BASE  # 取消变异 → 与快照一致
    out = await diff_memory(action="compare", handle=snap["handle"])
    assert out["changed_count"] == 0
    assert out["changes"] == []


async def test_unknown_handle_is_args_invalid(fake_diff):
    from ppsspp_dfx_mcp.errors import ToolError

    with pytest.raises(ToolError) as ei:
        await diff_memory(action="compare", handle="nope")
    assert "[ARGS_INVALID]" in str(ei.value)


async def test_drop_and_list_roundtrip(fake_diff):
    snap = await diff_memory(action="snapshot", start="0x08804000", end="0x08805000")
    listed = await diff_memory(action="list")
    assert listed["count"] == 1
    dropped = await diff_memory(action="drop", handle=snap["handle"])
    assert dropped["dropped"] is True
    again = await diff_memory(action="drop", handle=snap["handle"])
    assert again["dropped"] is False
    assert (await diff_memory(action="list"))["count"] == 0


async def test_fifo_eviction_at_capacity(fake_diff):
    for _ in range(diff_mod._MAX_SNAPSHOTS + 1):
        await diff_memory(action="snapshot", start="0x08804000", end="0x08804100")
    listed = await diff_memory(action="list")
    assert listed["count"] == diff_mod._MAX_SNAPSHOTS


async def test_fifo_evicts_oldest_handle(fake_diff):
    from ppsspp_dfx_mcp.errors import ToolError

    handles = []
    for _ in range(diff_mod._MAX_SNAPSHOTS):
        snap = await diff_memory(action="snapshot", start="0x08804000", end="0x08804100")
        handles.append(snap["handle"])
    evicted = handles[0]
    # 再拍一张，FIFO 逐出最旧的 handle
    await diff_memory(action="snapshot", start="0x08804000", end="0x08804100")
    with pytest.raises(ToolError) as ei:
        await diff_memory(action="compare", handle=evicted)
    assert "[ARGS_INVALID]" in str(ei.value)


async def test_compare_rejects_cross_session_handle(fake_diff):
    from ppsspp_dfx_mcp.errors import ToolError

    snap = await diff_memory(
        action="snapshot", start="0x08804000", end="0x08805000", session_id="sess-A"
    )
    with pytest.raises(ToolError) as ei:
        await diff_memory(action="compare", handle=snap["handle"], session_id="sess-B")
    assert "[ARGS_INVALID]" in str(ei.value)
    assert "sess-A" in str(ei.value)


async def test_range_validation(fake_diff):
    from ppsspp_dfx_mcp.errors import ToolError

    with pytest.raises(ToolError) as ei:
        await diff_memory(action="snapshot", start="0x08805000", end="0x08804000")
    assert "[ARGS_INVALID]" in str(ei.value)
