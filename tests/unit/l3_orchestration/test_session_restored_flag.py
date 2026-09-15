"""F-6(a) fix lock: disk-restored sessions carry the `restored` flag.

A session materialized from sessions.json (a previous server run left it
behind) must be distinguishable from one started fresh in this process —
the v4 round proved a restored session can silently answer resource
reads with real (possibly stale) game data. Locks:

- session_manager._load_sessions stamps extra["restored"]=True;
- start-created sessions do NOT carry the flag;
- SessionResponse surfaces it (restored: 0/1, mirroring `recovered`);
- the ppsspp:// resources echo it in their payloads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session import session_manager as sm
from ppsspp_dfx_mcp.views.session import SessionResponse

ISO = "Z:/fake/game.iso"


def _write_sessions_json(path: Path, sid: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        sid: {
            "session_id": sid,
            "iso_path": ISO,
            "pid": 4242,
            "ws_url": "ws://127.0.0.1:12345/debugger",
            "exec_count": 3,
            "ws_connected": True,
            "extra": {"ppsspp_version": {"version": "v1.20.4"}},
        }
    }, ensure_ascii=False), encoding="utf-8")


def test_load_sessions_stamps_restored_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    sid = "sess-restored-1"
    _write_sessions_json(tmp_path / "sessions.json", sid)
    monkeypatch.setattr(sm, "sessions_path", lambda: tmp_path / "sessions.json")

    sessions = sm._load_sessions()
    sess = sessions[sid]
    assert sess.extra.get("restored") is True
    # pre-existing extra keys survive the stamp
    assert sess.extra["ppsspp_version"]["version"] == "v1.20.4"


def test_session_response_surfaces_restored() -> None:
    sess = Session(
        session_id="s1", iso_path=ISO,
        extra={"restored": True},
    )
    assert SessionResponse.from_session(sess).restored == 1
    fresh = Session(session_id="s2", iso_path=ISO)
    assert SessionResponse.from_session(fresh).restored == 0


@pytest.mark.asyncio
async def test_resource_payload_carries_restored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ppsspp_dfx_mcp import resources as res_mod

    restored = Session(session_id="s1", iso_path=ISO,
                       extra={"restored": True})

    async def fake_require() -> str:
        return "s1"

    async def fake_get_state(session_id: str) -> Session:
        return restored

    class _FakeClient:
        async def game_status(self):
            return {"event": "game.status", "game": {"title": "T"}}

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_swt(session_id: str):
        yield _FakeClient(), object()

    monkeypatch.setattr(res_mod, "_require_single_session", fake_require)
    monkeypatch.setattr(res_mod, "session_client_with_transport", fake_swt)
    monkeypatch.setattr(res_mod.session_manager, "get_session_state",
                        fake_get_state)

    payload = await res_mod.game_state()
    assert payload["restored"] is True
    assert payload["session_id"] == "s1"
