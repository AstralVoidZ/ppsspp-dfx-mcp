"""W1/W2 regression: the events queue is bounded, and own echoes are not evidence.

W1: ``WsTransport._events_queue`` used to be unbounded, while every ticketless
frame — including the echo of each fire-and-forget command — was appended and
never discarded, so a long session grew it without limit. It is now bounded
(``_EVENTS_QUEUE_MAX``) and drops the oldest frame on overflow, matching the
GameStateObserver per-event queues.

W2: ``_recv_loop`` used to report EVERY unmatched frame to
``CallDiagnostics.note_broadcast``. That included the echo of a command sent via
``fire_and_forget``, which proves the peer answered us but is NOT independent
evidence that the event's producer is alive — so it could turn "the producer is
dead" into "our ticket pairing broke".

Delivery is deliberately unchanged: the frames themselves still flow to
``events`` (the legacy step path consumes ``cpu.stepping`` broadcasts from
there), so these fixes touch only the queue bound and the diagnostics
bookkeeping.

Anchors: report W1/W2; ``src/ppsspp_dfx_mcp/core/transport.py`` ``_put_event`` /
``_recv_loop`` / ``fire_and_forget``; ``core/call_diagnostics.py``
``note_broadcast``.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from collections.abc import Callable
from typing import Any

from websockets.protocol import State

from ppsspp_dfx_mcp.core.transport import (
    _EVENTS_QUEUE_MAX,
    _FIRE_AND_FORGET_TICKET_MEMORY,
    WS_SUBPROTOCOL,
    WsTransport,
)


class _FakeWs:
    """Minimal PPSSPP socket: whatever ``feed()`` pushes comes back from recv()."""

    def __init__(self) -> None:
        self.subprotocol = WS_SUBPROTOCOL
        self.state = State.OPEN
        self.sent: list[str] = []
        self._messages: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        # Queue-backed: the transport's ``wait_for(recv(), timeout=0.5)`` may
        # cancel this repeatedly, and a cancelled ``get()`` loses nothing.
        return await self._messages.get()

    async def close(self) -> None:
        self.state = State.CLOSED

    def feed(self, payload: dict[str, Any]) -> None:
        self._messages.put_nowait(json.dumps(payload))


async def _wait_until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


def _transport_with_recv_loop() -> tuple[WsTransport, _FakeWs, asyncio.Task[None]]:
    transport = WsTransport("127.0.0.1", 12345)
    ws = _FakeWs()
    transport.ws = ws  # the recv loop runs against the fake socket
    return transport, ws, asyncio.ensure_future(transport._recv_loop())


class TestEventsQueueIsBounded:
    """W1: a queue nobody drains must not grow without limit."""

    def test_queue_is_constructed_with_the_bound(self) -> None:
        transport = WsTransport("127.0.0.1", 1)
        assert transport.events.maxsize == _EVENTS_QUEUE_MAX

    def test_put_event_drops_the_oldest_frame(self) -> None:
        transport = WsTransport("127.0.0.1", 1)
        overflow = 10
        for seq in range(_EVENTS_QUEUE_MAX + overflow):
            transport._put_event({"event": "gpu.stats.get", "seq": seq})

        queue = transport.events
        assert queue.qsize() == _EVENTS_QUEUE_MAX
        assert transport._events_dropped == overflow
        # Drop-OLDEST: the surviving window starts `overflow` frames in.
        assert queue.get_nowait()["seq"] == overflow

    def test_requeue_honours_the_bound_instead_of_raising(self) -> None:
        transport = WsTransport("127.0.0.1", 1)
        for seq in range(_EVENTS_QUEUE_MAX):
            transport._put_event({"event": "log", "seq": seq})

        # wait_for_broadcast requeues its backlog in a finally block; the queue
        # may have refilled meanwhile (pre-fix this raised QueueFull, and
        # before the bound existed the backlog made the queue grow).
        transport._requeue(
            [
                {"event": "cpu.stepping", "seq": 1000},
                {"event": "cpu.stepping", "seq": 1001},
            ]
        )

        drained: list[dict[str, Any]] = []
        while not transport.events.empty():
            drained.append(transport.events.get_nowait())
        assert len(drained) == _EVENTS_QUEUE_MAX
        assert transport._events_dropped == 2
        assert [msg["seq"] for msg in drained[:2]] == [2, 3]
        assert [msg["seq"] for msg in drained[-2:]] == [1000, 1001]

    def test_recv_loop_feeds_the_queue_through_the_bounded_helper(self) -> None:
        source = inspect.getsource(WsTransport._recv_loop)
        assert "_put_event(" in source
        assert "_events_queue.put" not in source, (
            "the events queue must only be fed through the bounded _put_event()"
        )


class TestOwnEchoIsNotBroadcastEvidence:
    """W2: our own fire-and-forget echo must not evidence producer liveness."""

    async def test_echo_of_own_command_is_delivered_but_not_evidence(self) -> None:
        transport, ws, task = _transport_with_recv_loop()
        try:
            # A ticketed call on the same event name is still in flight: this
            # is the record a same-event broadcast would wrongly "rescue".
            record = transport.diagnostics.begin("cpu.stepping", "t-call")

            await transport.fire_and_forget("cpu.stepping")
            own_ticket = transport._ff_tickets[-1]
            ws.feed({"event": "cpu.stepping", "ticket": own_ticket})

            assert await _wait_until(lambda: not transport.events.empty())
            assert record.last_same_event_broadcast_at is None, (
                "an echo of our own fire-and-forget is not producer-liveness evidence"
            )
            # ...but the frame still reaches the queue (delivery unchanged).
            assert transport.events.get_nowait()["ticket"] == own_ticket

            # A genuine ticketless broadcast IS evidence.
            ws.feed({"event": "cpu.stepping", "pc": "0x08804000"})
            assert await _wait_until(lambda: record.last_same_event_broadcast_at is not None), (
                "a real broadcast must still be reported as evidence"
            )
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def test_fire_and_forget_ticket_memory_is_bounded(self) -> None:
        transport = WsTransport("127.0.0.1", 1)
        transport.ws = _FakeWs()
        for _ in range(_FIRE_AND_FORGET_TICKET_MEMORY + 5):
            await transport.fire_and_forget("cpu.resume")

        assert len(transport._ff_tickets) == _FIRE_AND_FORGET_TICKET_MEMORY
        assert "t1" not in transport._ff_tickets, (
            "aged-out fire-and-forget tickets must be evicted, not accumulated"
        )
