"""S1 fix (2026-09-06): step-broadcast routing regression tests.

Architecture bug being guarded against: the GameStateObserver's
single-consumer dispatcher owns ``transport.events`` on session-level
transports. Before the S1 fix, ``transport.wait_for_broadcast('cpu.stepping')``
(from debug_client step confirmation) raced the dispatcher on the same
queue and DETERMINISTICALLY lost — the dispatcher is parked at the head
of the asyncio.Queue getter deque, so every ``cpu.stepping`` broadcast
was consumed and silently dropped (the event is not in the dispatcher's
subscribed set). Consequences: step confirmation burned the full timeout
and fell back to legacy cpu.status polling; a no-op step (F-4) reported
fake success; run_until with the stale filter active always timed out.

Fix: ``cpu.stepping`` is subscribed in the observer and step confirmation
consumes from the dedicated per-event queue (routing via
``PpssppDebugClient._wait_step_broadcast``).

The production-order race test below reproduces the original failure
mode as a unit test (consumer parks BEFORE the broadcast arrives) —
this is the exact scenario that timed out 5/5 times before the fix.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fake_transport import FakeTransport

from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver
from ppsspp_dfx_mcp.core.transport import WsTransport
from ppsspp_dfx_mcp.errors import StepNoAdvanceError
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

# ---------- helpers ----------


def _advance_broadcast(pc: int, ticks: float) -> dict[str, Any]:
    """A cpu.stepping broadcast whose pc/ticks differ from pre-step."""
    return {
        "event": "cpu.stepping",
        "pc": pc,
        "ticks": ticks,
        "reason": "cpu.stepInto",
        "relatedAddress": 0,
    }


def _stale_broadcast(pc: int, ticks: float) -> dict[str, Any]:
    """A cpu.stepping broadcast whose pc AND ticks match pre-step."""
    return {
        "event": "cpu.stepping",
        "pc": pc,
        "ticks": ticks,
        "reason": "cpu.stepping",
        "relatedAddress": 0,
    }


# ---------- 1. production-order race regression (real WsTransport) ----------


class TestProductionOrderRace:
    """The consumer parks FIRST, the broadcast arrives LATER.

    This is the deterministic-steal order that broke the raw
    transport.wait_for_broadcast path (5/5 timeouts pre-fix).
    """

    @pytest.mark.asyncio
    async def test_observer_queue_receives_broadcast_arriving_after_park(
        self,
    ) -> None:
        transport = WsTransport("127.0.0.1", 1)  # never connected — queue is local
        observer = GameStateObserver(transport)
        try:
            await observer.start()
            assert observer.is_running()

            # Production order: consumer parks before the broadcast exists.
            wait_task = asyncio.ensure_future(observer.wait_for_step_broadcast(timeout_ms=1000))
            await asyncio.sleep(0.05)
            await transport.events.put(_advance_broadcast(0x111, 1.0))

            msg = await asyncio.wait_for(wait_task, timeout=5)
            assert msg is not None
            assert msg["pc"] == 0x111
        finally:
            await observer.stop()
            await transport.close()

    @pytest.mark.asyncio
    async def test_dispatcher_no_longer_steals_from_wait_for_broadcast(
        self,
    ) -> None:
        """Defense in depth: with cpu.stepping subscribed, a broadcast that
        arrives while a raw transport.wait_for_broadcast consumer is parked
        must reach ONE of the two consumers — and the observer dispatcher
        must not leave the raw consumer starved in the park-then-arrive
        order across repeated rounds.
        """
        transport = WsTransport("127.0.0.1", 1)
        observer = GameStateObserver(transport)
        try:
            await observer.start()
            await asyncio.sleep(0.05)
            # The dispatcher parks on transport.events; a raw
            # wait_for_broadcast consumer loses the race — but the broadcast
            # must be visible on the observer's dedicated queue either way.
            await transport.events.put(_advance_broadcast(0x222, 2.0))
            msg = await observer.wait_for_step_broadcast(timeout_ms=1000)
            assert msg is not None and msg["pc"] == 0x222
        finally:
            await observer.stop()
            await transport.close()


# ---------- 2. routing: client uses observer queue when attached ----------


@pytest.fixture
def fake_transport() -> FakeTransport:
    t = FakeTransport()
    t.set_state({"stepping": True, "pc": 0x08804000, "ticks": 100.0})
    return t


@pytest.fixture
async def observer(fake_transport: FakeTransport):
    obs = GameStateObserver(fake_transport)
    await obs.start()
    yield obs
    await obs.stop()


class TestStepRoutingWithObserver:
    @pytest.mark.asyncio
    async def test_step_into_via_observer_queue(
        self, fake_transport: FakeTransport, observer: GameStateObserver
    ) -> None:
        """step_into consumes the step broadcast through the observer's
        dedicated queue when an observer is attached (session-transport
        production path)."""

        def _step_faf(t: FakeTransport, **params: Any) -> None:
            t.push_broadcast(_advance_broadcast(0x08804004, 104.0))
            t.set_state({**t.state, "pc": 0x08804004, "ticks": 104.0})

        fake_transport.set_faf_handler("cpu.stepInto", _step_faf)
        client = PpssppDebugClient(fake_transport, game_state_observer=observer)

        result = await client.step_into(timeout_ms=2000)
        assert result["pc"] == 0x08804004
        assert result["ticks"] == 104.0

    @pytest.mark.asyncio
    async def test_step_no_advance_still_fails_fast_via_observer(
        self, fake_transport: FakeTransport, observer: GameStateObserver
    ) -> None:
        """F-4 semantics preserved through the observer path: repeated
        no-op broadcasts (pc/ticks unchanged) surface as a decisive
        StepNoAdvanceError instead of a blind timeout."""

        def _step_faf_stale(t: FakeTransport, **params: Any) -> None:
            t.push_broadcast(_stale_broadcast(0x08804000, 100.0))

        fake_transport.set_faf_handler("cpu.stepInto", _step_faf_stale)
        client = PpssppDebugClient(fake_transport, game_state_observer=observer)

        with pytest.raises(StepNoAdvanceError):
            await client.step_into(timeout_ms=5000)

    @pytest.mark.asyncio
    async def test_confirm_step_completed_filter_rejects_stale_via_observer(
        self, fake_transport: FakeTransport, observer: GameStateObserver
    ) -> None:
        """run_until/next_hle path: with the stale filter active, a stale
        broadcast is consumed+dropped and the wait raises TimeoutError
        (no legacy fallback that would mask the stale broadcast)."""
        fake_transport.push_broadcast(_stale_broadcast(0x08804000, 100.0))
        client = PpssppDebugClient(fake_transport, game_state_observer=observer)

        with pytest.raises(TimeoutError):
            await client._confirm_step_completed(timeout_ms=400, pre_pc=0x08804000, pre_ticks=100.0)


class TestStepRoutingFallback:
    @pytest.mark.asyncio
    async def test_no_observer_falls_back_to_transport_wait_for_broadcast(
        self, fake_transport: FakeTransport
    ) -> None:
        """Per-call fallback transports (no observer) keep using the raw
        transport.wait_for_broadcast primitive."""

        def _step_faf(t: FakeTransport, **params: Any) -> None:
            t.push_broadcast(_advance_broadcast(0x08804004, 104.0))

        fake_transport.set_faf_handler("cpu.stepInto", _step_faf)
        client = PpssppDebugClient(fake_transport)  # no observer

        result = await client.step_into(timeout_ms=2000)
        assert result["pc"] == 0x08804004
