"""R14 (design_ppsspp_dfx_mcp_test_refactor_v2 §R14): queue consumer
exclusivity — the generic regression pattern for the S1 class of bugs.

S1 root cause: the GameStateObserver dispatcher is the SOLE consumer of
``transport.events``; anything it does not subscribe to it consumes and
silently drops. Before the S1 fix, ``cpu.stepping`` was unsubscribed, so
step confirmation (transport.wait_for_broadcast) deterministically lost
every broadcast. That bug was only found by a one-off offline experiment.

This file turns the property into a permanent, generic check:

1. The subscribed-event set matches the EXPECTED tuple exactly (a drift
   tripwire — removing an event to "fix" a lost-broadcast symptom the
   wrong way turns this red immediately).
2. For every subscribed event: a broadcast injected into
   ``transport.events`` lands in the event's dedicated queue and leaves
   the transport queue drained (single-consumer routing intact).
3. Unsubscribed events are dropped by the dispatcher and reach NO
   dedicated queue (documented defensive behavior).
"""

from __future__ import annotations

import asyncio

import pytest

from ppsspp_dfx_mcp.core.game_state_observer import (
    _SUBSCRIBED_EVENTS,
    GameStateObserver,
)
from ppsspp_dfx_mcp.core.transport import WsTransport

# The ratified subscription set (R14). If you genuinely need a new
# subscribed event, extend this tuple AND the dispatch behavior together.
EXPECTED_SUBSCRIBED: frozenset[str] = frozenset({
    "game.start",
    "game.quit",
    "game.pause",
    "game.resume",
    "log",
    "cpu.resume",
    "cpu.stepping",
    "gpu.stats.get",
})


def test_subscription_set_matches_ratified_set() -> None:
    assert frozenset(_SUBSCRIBED_EVENTS) == EXPECTED_SUBSCRIBED, (
        "the observer's subscribed-event set drifted from the ratified set "
        "— an un-subscribed event's broadcasts are consumed and silently "
        "dropped by the dispatcher (the S1 failure mode)"
    )


@pytest.mark.asyncio
async def test_every_subscribed_event_routes_to_its_dedicated_queue(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = WsTransport("127.0.0.1", 1)  # never connected — queue is local
    observer = GameStateObserver(transport)
    try:
        await observer.start()
        assert observer.is_running()
        for event in sorted(EXPECTED_SUBSCRIBED):
            if event == "log":
                # Special case: the log queue has a BUILT-IN consumer
                # (_log_consumer injects broadcasts into Python logging).
                # Its routing contract is the log record, not queue access.
                import logging

                with caplog.at_level(
                    logging.INFO, logger="ppsspp_dfx_mcp.ppsspp_log"
                ):
                    await transport.events.put({
                        "event": "log", "level": 2,
                        "message": "R14-routing-probe",
                        "channel": "t", "header": "h",
                    })
                    for _ in range(40):
                        if any("R14-routing-probe" in r.getMessage()
                               for r in caplog.records):
                            break
                        await asyncio.sleep(0.05)
                    assert any(
                        "R14-routing-probe" in r.getMessage()
                        for r in caplog.records
                    ), "log broadcast never surfaced as a Python log record"
                assert transport.events.empty()
                continue
            await transport.events.put({"event": event, "probe": event})
            msg = await asyncio.wait_for(
                observer._queues[event].get(), timeout=2.0
            )
            assert msg["probe"] == event
            assert transport.events.empty(), (
                f"{event}: the dispatcher must be the only transport.events "
                "consumer, and routing must leave the transport queue drained"
            )
    finally:
        await observer.stop()
        await transport.close()


@pytest.mark.asyncio
async def test_unsubscribed_events_reach_no_dedicated_queue() -> None:
    transport = WsTransport("127.0.0.1", 1)
    observer = GameStateObserver(transport)
    try:
        await observer.start()
        await transport.events.put({"event": "cpu.breakpoint.hit", "probe": 1})
        await asyncio.sleep(0.1)
        assert transport.events.empty(), "dispatcher must consume everything"
        for event, queue in observer._queues.items():
            assert queue.empty(), (
                f"unsubscribed event leaked into the {event} queue"
            )
    finally:
        await observer.stop()
        await transport.close()
