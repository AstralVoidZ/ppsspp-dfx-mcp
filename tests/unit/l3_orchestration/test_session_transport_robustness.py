"""P4 review-fix regression tests (2026-09-06).

- W7: WsTransport.call() must not strand a pending future when the send
  fails, and the recv loop must survive malformed messages (previously a
  bad frame killed the loop while ws.state stayed OPEN — is_connected()
  stayed True, auto-reconnect was disabled, and every call hung until
  its own timeout).
- W8: gc_idle_sessions must release the session's WsTransport and
  GameStateObserver (previously only the launcher was popped — a leak
  for every GC'd session).
- W9: stale-session PID kills are refused when the PID now belongs to a
  non-PPSSPP process image (OS PID reuse could get unrelated process
  trees force-killed via taskkill /F /T).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.protocol import State

from ppsspp_dfx_mcp.core import proc
from ppsspp_dfx_mcp.core.transport import WsTransport
from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session import session_manager as sm

# ── W7: transport ────────────────────────────────────────────────────────


class _SendFailingWebSocket:
    """WS double whose send() raises after registration (disconnect race)."""

    def __init__(self) -> None:
        self.subprotocol = "debugger.ppsspp.org"
        self.state = State.OPEN

    async def recv(self) -> str:
        await asyncio.sleep(1.0)

    async def send(self, data: str) -> None:
        raise ConnectionError("send failed: socket closed")

    async def close(self) -> None:
        self.state = State.CLOSED


@pytest.mark.asyncio
async def test_w7_call_send_failure_does_not_leak_pending():
    """W7: a send failure pops the ticket from _pending and re-raises."""
    with patch(
        "ppsspp_dfx_mcp.core.transport.websockets.connect",
        new=_connect_returning(_SendFailingWebSocket()),
    ):
        t = WsTransport("127.0.0.1", 12345)
        await t.connect()
        with pytest.raises(ConnectionError, match="send failed"):
            await t.call("cpu.status")
        assert t._pending == {}, "W7: a failed send must not strand a future in _pending"
        await t.close()


class _MalformedThenLiveWebSocket:
    """WS double: one malformed frame, then ticketed echo responses."""

    def __init__(self) -> None:
        self.subprotocol = "debugger.ppsspp.org"
        self.state = State.OPEN
        self.sent: list[str] = []
        self._malformed_sent = False

    async def recv(self) -> str:
        if not self._malformed_sent:
            self._malformed_sent = True
            return "this is { not json"
        # Block until a send gives us something to echo.
        for _ in range(100):
            await asyncio.sleep(0.02)
            if self.sent:
                msg = json.loads(self.sent.pop(0))
                return json.dumps({"event": msg.get("event", "?"), "ticket": msg["ticket"]})
        raise ConnectionError("no more traffic")

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.state = State.CLOSED


def _connect_returning(ws):
    async def _connect(*args: Any, **kwargs: Any) -> Any:
        return ws

    return _connect


@pytest.mark.asyncio
async def test_w7_recv_loop_survives_malformed_message():
    """W7: a malformed frame is dropped and the transport keeps working."""
    ws = _MalformedThenLiveWebSocket()
    with patch(
        "ppsspp_dfx_mcp.core.transport.websockets.connect",
        new=_connect_returning(ws),
    ):
        t = WsTransport("127.0.0.1", 12345)
        await t.connect()
        result = await asyncio.wait_for(t.call("cpu.status"), timeout=5.0)
        assert result["event"] == "cpu.status"
        assert t.is_connected(), "W7: the recv loop must still be alive after a bad frame"
        await t.close()


# ── W8: GC releases transports/observers ────────────────────────────────


@pytest.mark.asyncio
async def test_w8_gc_pops_and_closes_transport_and_observer(tmp_path, monkeypatch):
    """W8: gc_idle_sessions releases the session's transport + observer."""
    monkeypatch.setattr(sm, "sessions_path", lambda: tmp_path / "sessions.json")

    sess = Session(
        session_id="sess-gone",
        iso_path="/test.iso",
        pid=123,
        ws_url="ws://127.0.0.1:1/debugger",
    )
    sm._save_sessions({"sess-gone": sess})

    mgr = sm.SessionManager()
    dead_transport = MagicMock()
    dead_transport.is_connected.return_value = False
    dead_transport.close = AsyncMock()
    observer = MagicMock()
    observer.stop = AsyncMock()
    mgr._transports["sess-gone"] = dead_transport
    mgr._observers["sess-gone"] = observer

    # Dead PID → the session is GC-eligible immediately.
    with (
        patch.object(proc, "is_pid_alive", return_value=False),
        patch.object(sm, "_kill_stale_pid_safe") as kill_mock,
    ):
        stopped = await mgr.gc_idle_sessions()

    assert "sess-gone" in stopped
    assert "sess-gone" not in mgr._transports, "W8: GC must pop the session transport"
    assert "sess-gone" not in mgr._observers, "W8: GC must pop the session observer"
    observer.stop.assert_awaited_once()
    dead_transport.close.assert_awaited_once()
    kill_mock.assert_called_once()


# ── W9: PID-reuse kill refusal ───────────────────────────────────────────


def test_w9_pid_name_check_refuses_non_ppsspp():
    """W9: _kill_stale_pid_safe refuses when the image is not PPSSPP."""
    with (
        patch.object(sm, "_pid_name_is_ppsspp", return_value=False),
        patch.object(sm, "_force_kill_pid") as kill_mock,
    ):
        sm._kill_stale_pid_safe(4242)
    kill_mock.assert_not_called()


def test_w9_pid_name_check_kills_when_unknown():
    """W9: indeterminate name (None) keeps the legacy kill-anyway path."""
    with (
        patch.object(sm, "_pid_name_is_ppsspp", return_value=None),
        patch.object(sm, "_force_kill_pid") as kill_mock,
    ):
        sm._kill_stale_pid_safe(4242)
    kill_mock.assert_called_once_with(4242)


@pytest.mark.asyncio
async def test_w9_stop_session_refuses_reused_pid(tmp_path, monkeypatch):
    """W9: stopping a disk-loaded session whose PID was reused by another
    image must not force-kill that process."""
    monkeypatch.setattr(sm, "sessions_path", lambda: tmp_path / "sessions.json")

    sess = Session(
        session_id="sess-stale",
        iso_path="/test.iso",
        pid=999999,
        ws_url="ws://127.0.0.1:1/debugger",
    )
    sm._save_sessions({"sess-stale": sess})

    mgr = sm.SessionManager()
    with (
        patch.object(proc, "is_pid_alive", return_value=True),
        patch.object(sm, "_pid_name_is_ppsspp", return_value=False),
        patch.object(sm, "_force_kill_pid") as kill_mock,
    ):
        await mgr.stop_session("sess-stale")

    kill_mock.assert_not_called()
    # The session is still removed from the registry either way.
    assert "sess-stale" not in sm._load_sessions()
