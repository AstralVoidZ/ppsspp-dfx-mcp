"""R4 (design_ppsspp_dfx_mcp_test_refactor_v1 §R4): transport resilience.

Regression anchor: verification report F-3 — the session-level
WsTransport had NO reconnect; one dropped connection (e.g. an oversized
``memory.readString`` response) poisoned every subsequent call until the
server restarted. F-3 fix: ``call()`` / ``fire_and_forget()`` attempt one
reconnect via ``_ensure_connected()`` before failing.

These tests pin the new contract with the same MockWebSocket pattern as
test_transport_orchestration.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest
from websockets.protocol import State

from ppsspp_dfx_mcp.core.transport import WsTransport


class MockWebSocket:
    """WS double that auto-echoes a ticketed response for every send."""

    def __init__(self) -> None:
        self.subprotocol = "debugger.ppsspp.org"
        self.state = State.OPEN
        self.sent: list[str] = []
        self.incoming: list[str] = []

    async def recv(self) -> str:
        if self.incoming:
            return self.incoming.pop(0)
        await asyncio.sleep(1.0)

    async def send(self, data: str) -> None:
        self.sent.append(data)
        msg = json.loads(data)
        self.incoming.append(
            json.dumps({"event": msg.get("event", "unknown"), "ticket": msg["ticket"]})
        )

    async def close(self) -> None:
        self.state = State.CLOSED


def _patch_connect_seq(mocks: list[MockWebSocket]):
    """Patch websockets.connect to hand out the given mocks in order."""
    it = iter(mocks)

    async def _connect(*args: Any, **kwargs: Any) -> MockWebSocket:
        return next(it)

    return patch("ppsspp_dfx_mcp.core.transport.websockets.connect", new=_connect)


@pytest.mark.asyncio
async def test_call_reconnects_after_drop():
    """F-3 contract: a dropped WS is reconnected transparently mid-call."""
    dead = MockWebSocket()
    fresh = MockWebSocket()
    # connect() runs once at setup (dead), then again inside
    # _ensure_connected (fresh) — hence a two-element sequence.
    with _patch_connect_seq([dead, fresh]):
        t = WsTransport("127.0.0.1", 12345)
        await t.connect()

        # First call succeeds on the original connection.
        r1 = await t.call("cpu.status")
        assert r1["ticket"] == "t1"

        # The connection dies.
        dead.state = State.CLOSED

        # Next call must reconnect to the fresh socket and succeed. W3 fix
        # (2026-09-06): the reconnect first runs the version handshake, so
        # the handshake consumes ticket t2 and the original call is t3.
        result = await asyncio.wait_for(t.call("cpu.status"), timeout=5.0)
        assert result["ticket"] == "t3"
        sent_events = [json.loads(m).get("event") for m in fresh.sent]
        assert sent_events == ["version", "cpu.status"]


@pytest.mark.asyncio
async def test_call_raises_when_reconnect_fails():
    """F-3 contract: reconnect failure surfaces a descriptive RuntimeError
    (not a silent hang, not a context-free crash)."""

    async def _fail_connect(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionRefusedError("refused")

    with patch("ppsspp_dfx_mcp.core.transport.websockets.connect", new=_fail_connect):
        t = WsTransport("127.0.0.1", 12345)
        with pytest.raises(RuntimeError, match="not connected"):
            await t.call("cpu.status")


@pytest.mark.asyncio
async def test_fire_and_forget_reconnects_after_drop():
    """F-3 contract: fire_and_forget also reconnects."""
    dead = MockWebSocket()
    fresh = MockWebSocket()
    with _patch_connect_seq([dead, fresh]):
        t = WsTransport("127.0.0.1", 12345)
        await t.connect()
        dead.state = State.CLOSED
        await asyncio.wait_for(t.fire_and_forget("cpu.stepInto"), timeout=5.0)
        assert any('"cpu.stepInto"' in m for m in fresh.sent)
