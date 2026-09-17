"""H1 fan-out invariants: cpu.stepping subscribers never steal from each
other nor from the legacy step-confirmation queue.

Ratified design (analysis_ppsspp_dfx_mcp_tool_layering_v1 §3 裁决 2): the
dedicated per-event queue remains the step-confirmation buffer; the
dispatcher additionally fans every cpu.stepping broadcast out to
registered SteppingSubscription instances (own bounded queue, drop-oldest).
"""

from __future__ import annotations

import asyncio

import pytest

from ppsspp_dfx_mcp.core.game_state_observer import (
    GameStateObserver,
    SteppingSubscription,
)


class _EventQueueTransport:
    """Minimal transport stub: the dispatcher only consumes .events; the
    defensive broadcast.config.set call at start() goes through call()."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[dict] = asyncio.Queue()

    async def call(self, event: str, **params):
        return {}


async def _pump() -> None:
    # Let the dispatcher coroutine forward queued events.
    for _ in range(5):
        await asyncio.sleep(0.01)


def _push(transport: _EventQueueTransport, n: int) -> None:
    for i in range(n):
        transport.events.put_nowait({"event": "cpu.stepping", "pc": 0x08800000 + i, "seq": f"b{i}"})


async def _drain(sub: SteppingSubscription, n: int) -> list[dict]:
    out = []
    for _ in range(n):
        msg = await sub.get(timeout_s=0.2)
        assert msg is not None, "subscriber lost a broadcast"
        out.append(msg)
    return out


@pytest.mark.asyncio
async def test_fanout_two_subscribers_each_get_every_broadcast():
    transport = _EventQueueTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    try:
        sub1 = observer.subscribe_stepping()
        sub2 = observer.subscribe_stepping()
        _push(transport, 3)
        await _pump()
        got1 = await _drain(sub1, 3)
        got2 = await _drain(sub2, 3)
        assert [m["seq"] for m in got1] == ["b0", "b1", "b2"]
        assert [m["seq"] for m in got2] == ["b0", "b1", "b2"]
    finally:
        await observer.stop()


@pytest.mark.asyncio
async def test_fanout_stalled_subscriber_drops_oldest_not_block():
    """A stalled (never-consuming) subscriber must not block the
    dispatcher nor starve a concurrently-consuming peer (I2 isolation):
    the active one sees every broadcast as it flows; the stalled one
    keeps only the newest queue-cap messages."""
    transport = _EventQueueTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    try:
        stalled = observer.subscribe_stepping()
        active = observer.subscribe_stepping()
        got: list[str] = []

        async def consume() -> None:
            while True:
                m = await active.get(timeout_s=0.3)
                if m is None:
                    return
                got.append(m["seq"])

        consumer = asyncio.create_task(consume())
        # Feed in small batches so the consumer drains while production
        # is still flowing (a >cap burst consumed only afterwards would
        # legitimately hit the bounded-backlog cap — see next test).
        for i in range(40):
            transport.events.put_nowait({"event": "cpu.stepping", "pc": i, "seq": f"b{i}"})
            if i % 4 == 0:
                await asyncio.sleep(0.005)
        await consumer
        assert got == [f"b{i}" for i in range(40)]
        # Stalled subscriber kept only the newest 32 (drop-oldest).
        assert stalled._queue.qsize() == 32
        newest_first = await stalled.get(timeout_s=0.1)
        assert newest_first is not None and newest_first["seq"] == "b8"
    finally:
        await observer.stop()


@pytest.mark.asyncio
async def test_fanout_burst_over_cap_keeps_newest_window():
    """Bounded backlog: a burst larger than the queue cap that is only
    consumed AFTER production finished leaves each subscriber the newest
    32 broadcasts — and never blocks or crashes the dispatcher."""
    transport = _EventQueueTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    try:
        sub1 = observer.subscribe_stepping()
        sub2 = observer.subscribe_stepping()
        _push(transport, 40)  # > default maxsize 32
        await _pump()
        for sub in (sub1, sub2):
            first = await sub.get(timeout_s=0.1)
            assert first is not None and first["seq"] == "b8"
            assert sub._queue.qsize() == 31  # 32-message window minus the one consumed
    finally:
        await observer.stop()


@pytest.mark.asyncio
async def test_fanout_subscribers_and_legacy_step_queue_coexist():
    """The legacy step-confirmation queue AND subscriber queues are fed
    independently (A-H1-3): neither consumer can steal the other's
    broadcast."""
    transport = _EventQueueTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    try:
        sub = observer.subscribe_stepping()
        _push(transport, 2)
        await _pump()
        got = await _drain(sub, 2)
        assert [m["seq"] for m in got] == ["b0", "b1"]
        # Legacy queue still holds its own copies for step confirmation.
        legacy_msg = await observer.wait_for_step_broadcast(timeout_ms=200)
        assert legacy_msg is not None and legacy_msg["seq"] == "b0"
    finally:
        await observer.stop()


@pytest.mark.asyncio
async def test_subscription_close_detaches_from_dispatcher():
    transport = _EventQueueTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    sub = observer.subscribe_stepping()
    async with sub as ctx:
        assert ctx is sub
        assert observer._stepping_subscribers == [sub]
    assert observer._stepping_subscribers == []
    _push(transport, 3)
    await _pump()
    assert await sub.get(timeout_s=0.05) is None
    assert sub._queue.qsize() == 0
    # close() is idempotent.
    sub.close()
    await observer.stop()


@pytest.mark.asyncio
async def test_fanout_drain_drops_stale_backlog():
    """drain() discards broadcasts buffered before the caller finished
    arming (same intent as the W2 drain_resume fix)."""
    transport = _EventQueueTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    try:
        sub = observer.subscribe_stepping()
        _push(transport, 2)
        await _pump()
        assert sub.drain() == 2
        assert sub.drain() == 0
        _push(transport, 1)
        await _pump()
        msg = await sub.get(timeout_s=0.1)
        assert msg is not None and msg["seq"] == "b0"
    finally:
        await observer.stop()
