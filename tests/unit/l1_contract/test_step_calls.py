"""L1 contract tests for stepping methods (fire-and-forget + confirmation).

Anchors:
- cpu.stepInto / stepOver / stepOut: SteppingSubscriber.cpp:L56-60, L92-268
- cpu.runUntil: SteppingSubscriber.cpp (address param)
- cpu.nextHLE: SteppingSubscriber.cpp
- SteppingBroadcaster.cpp:L25-44, L57-72: cpu.stepping broadcast event
  pushed when step completes (carries pc/ticks/reason/relatedAddress).

Each step method pairs a fire-and-forget call with a `wait_for_broadcast`
subscription that waits for the `cpu.stepping` broadcast from PPSSPP's
SteppingBroadcaster. Default timeout_ms=5000 per the V004 redesign spec
(B.2 §2.4).
"""

from __future__ import annotations

from typing import Any

import pytest


def _push_stepping_broadcast_on_faf(transport: Any, **params: Any) -> None:
    """FakeTransport faf handler: push a cpu.stepping broadcast to the events queue.

    Simulates PPSSPP's SteppingBroadcaster.cpp:L57-72 pushing a
    `cpu.stepping` event when the CPU re-enters CORE_STEPPING after
    a step completes. The broadcast carries pc/ticks/reason/relatedAddress
    fields (SteppingBroadcaster.cpp:L25-44).
    """
    transport.push_broadcast({
        "event": "cpu.stepping",
        "pc": 0x08804000,
        "ticks": 12345.0,
        "reason": "cpu.stepInto",
        "relatedAddress": 0,
    })


class TestStepContract:
    """L1 contract: step methods fire-and-forget + confirm via cpu.stepping broadcast."""

    @pytest.mark.asyncio
    async def test_step_into_fires_cpu_stepInto_and_confirms(
        self, client, transport
    ):
        """L1 anchor: step_into fires `cpu.stepInto` (no params) + confirms.

        See SteppingSubscriber.cpp:L56-60, L92-268.
        """
        transport.set_faf_handler("cpu.stepInto", _push_stepping_broadcast_on_faf)
        result = await client.step_into(timeout_ms=500, interval_ms=10)

        assert transport.fire_and_forget_calls[-1][0] == "cpu.stepInto"
        assert transport.fire_and_forget_calls[-1][1] == {}
        # Confirmation: wait_for_broadcast returned the cpu.stepping
        # broadcast dict (SteppingBroadcaster.cpp:L25-44, L57-72).
        assert result.get("event") == "cpu.stepping"

    @pytest.mark.asyncio
    async def test_step_over_fires_cpu_stepOver_and_confirms(
        self, client, transport
    ):
        """L1 anchor: step_over fires `cpu.stepOver` (no params) + confirms.

        See SteppingSubscriber.cpp:L56-60, L92-268.
        """
        transport.set_faf_handler("cpu.stepOver", _push_stepping_broadcast_on_faf)
        result = await client.step_over(timeout_ms=500, interval_ms=10)

        assert transport.fire_and_forget_calls[-1][0] == "cpu.stepOver"
        assert transport.fire_and_forget_calls[-1][1] == {}
        assert result.get("event") == "cpu.stepping"

    @pytest.mark.asyncio
    async def test_step_out_fires_cpu_stepOut_and_confirms(
        self, client, transport
    ):
        """L1 anchor: step_out fires `cpu.stepOut` (no params) + confirms.

        See SteppingSubscriber.cpp:L56-60, L92-268.
        """
        transport.set_faf_handler("cpu.stepOut", _push_stepping_broadcast_on_faf)
        result = await client.step_out(timeout_ms=500, interval_ms=10)

        assert transport.fire_and_forget_calls[-1][0] == "cpu.stepOut"
        assert transport.fire_and_forget_calls[-1][1] == {}
        assert result.get("event") == "cpu.stepping"

    @pytest.mark.asyncio
    async def test_run_until_fires_cpu_runUntil_with_address(
        self, client, transport
    ):
        """L1 anchor: run_until fires `cpu.runUntil` with address param.

        See SteppingSubscriber.cpp:L56-60, L92-268 — address is the
        target PC to run until.
        """
        transport.set_faf_handler("cpu.runUntil", _push_stepping_broadcast_on_faf)
        result = await client.run_until(
            0x08804000, timeout_ms=500, interval_ms=10
        )

        assert transport.fire_and_forget_calls[-1][0] == "cpu.runUntil"
        assert transport.fire_and_forget_calls[-1][1] == {
            "address": 0x08804000
        }
        assert result.get("event") == "cpu.stepping"

    @pytest.mark.asyncio
    async def test_next_hle_fires_cpu_nextHLE_and_confirms(
        self, client, transport
    ):
        """L1 anchor: next_hle fires `cpu.nextHLE` (no params) + confirms.

        See SteppingSubscriber.cpp:L56-60, L92-268.
        """
        transport.set_faf_handler("cpu.nextHLE", _push_stepping_broadcast_on_faf)
        result = await client.next_hle(timeout_ms=500, interval_ms=10)

        assert transport.fire_and_forget_calls[-1][0] == "cpu.nextHLE"
        assert transport.fire_and_forget_calls[-1][1] == {}
        assert result.get("event") == "cpu.stepping"
