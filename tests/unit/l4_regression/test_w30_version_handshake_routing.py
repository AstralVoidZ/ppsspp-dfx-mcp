"""W30 regression: a live observer dispatcher must not swallow the handshake reply.

Pre-fix, ``_recv_loop`` pushed a ticketless ``event="version"`` reply onto
``transport.events`` — the same queue the ``GameStateObserver`` dispatcher
drains — and the dispatcher drops event names it is not subscribed to. On a
reconnect the dispatcher is already parked on that queue, so it wins the race
deterministically: ``send_version`` burns its whole budget, ``_ensure_connected``
reports a failed reconnect, and every later call on that session fails. The
reply now has its own single-consumer queue, so the race cannot exist.

Anchors: report W30; ``src/ppsspp_dfx_mcp/core/transport.py`` ``_recv_loop`` /
``send_version`` / ``__init__``; ``core/game_state_observer.py`` ``_dispatcher``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from websockets.protocol import State

from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver
from ppsspp_dfx_mcp.core.transport import WS_SUBPROTOCOL, WsTransport


class _TicketlessVersionWs:
    """Fake PPSSPP socket: replies to the version event WITHOUT a ticket.

    That is the upstream behaviour the handshake's fallback path exists for (see
    ``send_version``'s docstring), and the only case where the reply is
    ticketless and therefore queue-routed.
    """

    def __init__(self, version: str = "v1.20") -> None:
        self.subprotocol = WS_SUBPROTOCOL
        self.state = State.OPEN
        self.sent: list[str] = []
        self._version = version
        self._messages: list[str] = []
        self._ready = asyncio.Event()

    async def send(self, data: str) -> None:
        self.sent.append(data)
        if json.loads(data).get("event") == "version":
            asyncio.get_running_loop().call_later(0.05, self._reply)

    async def recv(self) -> str:
        # Event-based rather than queue-based so the transport's
        # ``wait_for(recv(), timeout=0.5)`` can cancel this repeatedly without
        # ever dropping a reply.
        while not self._messages:
            self._ready.clear()
            await self._ready.wait()
        return self._messages.pop(0)

    async def close(self) -> None:
        self.state = State.CLOSED

    def _reply(self) -> None:
        self._messages.append(
            json.dumps({"event": "version", "name": "PPSSPP", "version": self._version})
        )
        self._ready.set()


def _patch_connect(ws: _TicketlessVersionWs):
    """Patch ``websockets.connect`` to hand back ``ws``."""

    async def _connect(*args: Any, **kwargs: Any) -> _TicketlessVersionWs:
        return ws

    return patch("ppsspp_dfx_mcp.core.transport.websockets.connect", new=_connect)


def _silence_broadcast_config(transport: WsTransport) -> None:
    """Answer ``broadcast.config.set`` locally so ``observer.start()`` is fast.

    The version event must still go through the real ``call()``: the handshake
    has to actually send it for the fake socket to reply.
    """
    real_call = transport.call

    async def call(event: str, timeout: float = 5.0, **params: Any) -> dict[str, Any]:
        if event == "broadcast.config.set":
            return {}
        return await real_call(event, timeout=timeout, **params)

    transport.call = call  # type: ignore[method-assign]


async def _wait_until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


class TestW30VersionHandshakeRouting:
    """The handshake must complete while a real dispatcher is draining `events`."""

    async def test_dispatcher_does_not_steal_version_reply(self) -> None:
        ws = _TicketlessVersionWs()
        # Budget 1.5s → 0.75s ticket slice + 0.75s fallback window.
        transport = WsTransport("127.0.0.1", 12345, handshake_timeout_s=1.5)
        observer = GameStateObserver(transport)
        try:
            with _patch_connect(ws):
                await transport.connect()
                _silence_broadcast_config(transport)
                await observer.start()

                # Positive control: the dispatcher must actually be draining
                # `events`, otherwise this test would pass even with the
                # pre-fix routing.
                transport.events.put_nowait({"event": "game.start"})
                assert await _wait_until(lambda: transport.events.empty()), (
                    "dispatcher never drained the events queue"
                )
                assert observer.get_state() == "running"

                result = await transport.send_version()
        finally:
            await observer.stop()
            await transport.close()

        assert result["event"] == "version"
        assert result["name"] == "PPSSPP"
        assert transport.version_info is not None
        # Neither queue retains the handshake reply.
        assert transport.events.empty()
        assert transport._version_queue.empty()  # noqa: SLF001 — regression anchor

    async def test_stale_reply_is_not_returned_as_this_handshake(self) -> None:
        """A leftover reply from an earlier handshake must be discarded."""
        ws = _TicketlessVersionWs(version="v1.20.4-fresh")
        transport = WsTransport("127.0.0.1", 12345, handshake_timeout_s=1.5)
        try:
            with _patch_connect(ws):
                await transport.connect()
                transport._version_queue.put_nowait(  # noqa: SLF001 — seed the stale reply
                    {"event": "version", "name": "PPSSPP", "version": "v0-stale"}
                )
                result = await transport.send_version()
        finally:
            await transport.close()

        assert result["version"] == "v1.20.4-fresh"
        assert transport.version_info == result

    async def test_version_broadcast_does_not_reach_the_events_queue(self) -> None:
        """Routing anchor: the ticketless reply bypasses `events` entirely."""
        ws = _TicketlessVersionWs()
        transport = WsTransport("127.0.0.1", 12345, handshake_timeout_s=1.5)
        try:
            with _patch_connect(ws):
                await transport.connect()
                await transport.send_version()
                await asyncio.sleep(0.05)
        finally:
            await transport.close()

        assert transport.events.empty()
