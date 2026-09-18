"""ppsspp_context contract tests — identity resolution, degradation paths,
and the disasm window shape, all against fakes (no PPSSPP)."""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp.tools import context as context_mod
from ppsspp_dfx_mcp.tools.context import context

KNOWN = {"game_main": 0x08804000, "draw_hook": 0x08810000}


class FakeClient:
    async def memory_map(self) -> dict:
        return {"ranges": [{"start": "0x08804000", "end": "0x08D34000", "type": "code"}]}

    async def disasm(self, address: int, count: int) -> list[dict]:
        return [{"address": hex(address + i * 4), "text": f"instr_{i}"} for i in range(count)]

    def with_stepping(self):
        return _FakeStepping()

    async def backtrace(self) -> dict:
        return {"frames": []}  # 暂停点无栈帧 → note 应说明为空


class _FakeStepping:
    def __init__(self) -> None:
        self.entered = False

    async def __aenter__(self) -> _FakeStepping:
        self.entered = True
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class FakeSessionClient:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    async def __aenter__(self) -> FakeClient:
        return self._client

    async def __aexit__(self, *exc) -> None:
        return None


@pytest.fixture()
def fake_context(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        context_mod, "session_client", lambda params: FakeSessionClient(FakeClient())
    )
    monkeypatch.setattr(context_mod, "_known_functions_runtime", lambda: dict(KNOWN))
    monkeypatch.setattr(context_mod, "resolve_session_id", _resolve)
    return FakeClient()


async def _resolve(session_id: str | None) -> str:
    return session_id or "sess-fake"


async def test_identity_resolves_nearest_known_function(fake_context):
    r = await context(address="0x08804100", session_id="sess-fake")
    assert r["identity"]["name"] == "game_main"
    assert r["identity"]["start"] == "0x08804000"
    assert r["identity"]["offset"] == 0x100


async def test_unknown_address_degrades_to_null_identity(fake_context):
    # 低于全部 known_functions 起点的地址 → identity=None（降级不报错）
    r = await context(address="0x087FFFFF", session_id="sess-fake")
    assert r["identity"] is None
    assert len(r["disasm"]) > 0


async def test_disasm_window_shape(fake_context):
    r = await context(address="0x08804010", window=4, session_id="sess-fake")
    assert len(r["disasm"]) == 9  # 2*window+1


async def test_window_out_of_range_rejected(fake_context):
    with pytest.raises(context_mod.ArgsInvalid, match="window must be in"):
        await context(address="0x08804000", window=0, session_id="sess-fake")
    with pytest.raises(context_mod.ArgsInvalid, match="window must be in"):
        await context(address="0x08804000", window=64, session_id="sess-fake")


async def test_backtrace_skipped_note_present(fake_context):
    r = await context(address="0x08804000", include_backtrace=True, session_id="sess-fake")
    assert r["backtrace_note"]
