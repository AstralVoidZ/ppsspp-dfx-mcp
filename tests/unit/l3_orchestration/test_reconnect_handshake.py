"""W3 fix test: auto-reconnect re-sends the version handshake.

Every first-connect call site pairs connect() with send_version(), and the
protocol header documents the handshake as REQUIRED on every new
connection — but _ensure_connected's reconnect path skipped it (the only
exception). Real-PPSSPP probes showed calls still succeed without the
handshake, so the fix is protocol hygiene: registration state stays
consistent across reconnects.

The test drives a real WsTransport against patched websockets.connect
echo doubles: after a simulated drop, the SECOND connection's first sent
message must be the version event, before the original call proceeds.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from websockets.protocol import State

from ppsspp_dfx_mcp.core.transport import WsTransport


class _EchoWebSocket:
    """WS double that answers each sent message with a ticket echo."""

    def __init__(self, sent_log: list[list[dict[str, Any]]]) -> None:
        self.subprotocol = "debugger.ppsspp.org"
        self.state = State.OPEN
        self.sent: list[dict[str, Any]] = []
        self._sent_log = sent_log
        sent_log.append(self.sent)  # one entry per connection, in order
        self._reply: asyncio.Queue[str] = asyncio.Queue()

    async def recv(self) -> str:
        return await self._reply.get()

    async def send(self, data: str) -> None:
        msg = json.loads(data)
        self.sent.append(msg)
        await self._reply.put(
            json.dumps({"event": msg["event"], "ticket": msg["ticket"]})
        )

    async def close(self) -> None:
        self.state = State.CLOSED


def _connect_factory(budget: list[_EchoWebSocket]):
    async def _connect(*args: Any, **kwargs: Any) -> _EchoWebSocket:
        ws = budget.pop(0)
        return ws

    return _connect


@pytest.mark.asyncio
async def test_reconnect_sends_version_before_original_call():
    log: list[list[dict[str, Any]]] = []
    ws1 = _EchoWebSocket(log)
    ws2 = _EchoWebSocket(log)
    budget = [ws1, ws2]

    from unittest.mock import patch

    with patch(
        "ppsspp_dfx_mcp.core.transport.websockets.connect",
        new=_connect_factory(budget),
    ):
        t = WsTransport("127.0.0.1", 12345)
        await t.connect()
        assert t.ws is ws1

        # Simulate a drop without going through close() (which clears the
        # recv task cleanly) — flip the state like a dead socket would.
        ws1.state = State.CLOSED
        assert not t.is_connected()

        resp = await asyncio.wait_for(t.call("cpu.status", timeout=5.0), 10.0)
        assert resp["event"] == "cpu.status"

    # Two connections were made, and the reconnect (ws2) sent the version
    # handshake FIRST — before the original cpu.status call.
    assert len(log) == 2
    assert ws1.sent == []  # nothing was ever sent on the dropped connection
    assert [m["event"] for m in ws2.sent] == ["version", "cpu.status"]


@pytest.mark.asyncio
async def test_reconnect_failure_wraps_handshake_error():
    """A version-handshake failure on reconnect must surface as the same
    RuntimeError wrapper as a connect failure (not leak a raw exception)."""
    log: list[list[dict[str, Any]]] = []

    class _HandshakeFailingWebSocket(_EchoWebSocket):
        async def send(self, data: str) -> None:
            msg = json.loads(data)
            if msg["event"] == "version":
                raise ConnectionError("handshake rejected")
            await super().send(data)

    ws1 = _EchoWebSocket(log)
    ws2 = _HandshakeFailingWebSocket(log)
    budget = [ws1, ws2]

    from unittest.mock import patch

    with patch(
        "ppsspp_dfx_mcp.core.transport.websockets.connect",
        new=_connect_factory(budget),
    ):
        t = WsTransport("127.0.0.1", 12345)
        await t.connect()
        ws1.state = State.CLOSED
        import pytest as _pytest

        with _pytest.raises(RuntimeError, match="reconnect .* failed"):
            await t.call("cpu.status", timeout=5.0)
