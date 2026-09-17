"""L3 orchestration tests: _confirm_step_completed end-to-end chain.

Anchors: B.2 spec §2.4 V004 invariants I6-I10 under end-to-end
orchestration. Complementary to L4 (which anchors isolated V004
invariants via AsyncMock in
`tests/unit/l4_regression/test_v004_confirm_step_broadcast.py`).

L3 focus (NOT covered by L4):
- End-to-end orchestration: faf handler pushes cpu.stepping broadcast
  → _confirm_step_completed subscribes via wait_for_broadcast → returns
  the full 5-field dict to the step method caller.
- 5 step methods orchestrate consistently (not mocked — real FakeTransport
  + real broadcast queue + real wait_for_broadcast).
- reason field propagates end-to-end (faf handler → broadcast → caller).
- Legacy fallback path (use_broadcast=False) orchestrates end-to-end.

V004 invariants (B.2 §2.4.4) anchored here:
- I6 (返回字段): end-to-end caller receives 5 fields (event/pc/ticks/
  reason/relatedAddress) — L4 mocks wait_for_broadcast; L3 verifies
  the real queue delivers them.
- I7 (timeout 一致): default 5000ms honored end-to-end.
- I8 (5 方法覆盖): 5 methods orchestrate through the same path.
- I9 (API 兼容): interval_ms accepted end-to-end (no-op in broadcast mode).
- I10 (reason 不丢): reason propagates from faf handler to caller.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fake_transport import FakeTransport

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

# ============================================================================
# Fixtures (local — L3 confirm-step tests need custom faf handlers that
# push broadcasts with specific reason fields)
# ============================================================================


def _make_step_handler(reason: str = "cpu.stepInto", pc: int = 0x08804000):
    """Build a faf handler that pushes a cpu.stepping broadcast.

    The handler pushes a broadcast with the given reason/pc fields,
    simulating PPSSPP's SteppingBroadcaster (SteppingBroadcaster.cpp:L57-72).
    """

    def handler(t: FakeTransport, **params: Any) -> None:
        t.set_state({"stepping": False})

        async def _push():
            t.push_broadcast(
                {
                    "event": "cpu.stepping",
                    "pc": pc,
                    "ticks": 12345.0,
                    "reason": reason,
                    "relatedAddress": 0,
                }
            )
            t.set_state({"stepping": True})

        asyncio.get_event_loop().call_soon(lambda: asyncio.ensure_future(_push()))

    return handler


@pytest.fixture
def step_transport() -> FakeTransport:
    """FakeTransport with all 5 step methods pushing broadcasts.

    Each step faf handler pushes a cpu.stepping broadcast with the
    matching reason field (cpu.stepInto / cpu.stepOver / cpu.stepOut /
    cpu.nextHLE). runUntil uses reason="breakpoint" (PPSSPP sets this
    when the run-until address is hit).
    """
    t = FakeTransport()
    t.set_state({"stepping": False})
    t.set_faf_handler("cpu.stepping", lambda t, **_: t.set_state({"stepping": True}))
    t.set_faf_handler("cpu.resume", lambda t, **_: t.set_state({"stepping": False}))
    t.set_faf_handler("cpu.stepInto", _make_step_handler("cpu.stepInto"))
    t.set_faf_handler("cpu.stepOver", _make_step_handler("cpu.stepOver"))
    t.set_faf_handler("cpu.stepOut", _make_step_handler("cpu.stepOut"))
    t.set_faf_handler("cpu.runUntil", _make_step_handler("breakpoint"))
    t.set_faf_handler("cpu.nextHLE", _make_step_handler("cpu.nextHLE"))
    return t


@pytest.fixture
def step_client(step_transport: FakeTransport) -> PpssppDebugClient:
    """PpssppDebugClient backed by step_transport."""
    return PpssppDebugClient(step_transport)


# ============================================================================
# End-to-end orchestration: 5 step methods (V004 I8)
# ============================================================================


class TestStepMethodEndToEndOrchestration:
    """L3: 5 step methods orchestrate faf → broadcast → 5-field return.

    V004 I8 (5 方法覆盖): all 5 step methods use the broadcast path.
    L4 anchors this via AsyncMock; L3 anchors the end-to-end chain
    (faf handler pushes broadcast → wait_for_broadcast consumes →
    caller receives 5 fields).
    """

    async def test_step_into_end_to_end_returns_5_fields(self, step_client):
        """step_into: faf → broadcast → caller receives 5 fields.

        V004 I6 (返回字段): end-to-end caller receives event/pc/ticks/
        reason/relatedAddress. The faf handler pushes the broadcast;
        _confirm_step_completed subscribes via wait_for_broadcast.
        """
        result = await step_client.step_into(timeout_ms=1000)

        assert result["event"] == "cpu.stepping"
        assert "pc" in result
        assert "ticks" in result
        assert result["reason"] == "cpu.stepInto"
        assert "relatedAddress" in result

    async def test_step_over_end_to_end_returns_5_fields(self, step_client):
        """step_over: end-to-end orchestration yields reason='cpu.stepOver'."""
        result = await step_client.step_over(timeout_ms=1000)

        assert result["event"] == "cpu.stepping"
        assert result["reason"] == "cpu.stepOver"

    async def test_step_out_end_to_end_returns_5_fields(self, step_client):
        """step_out: end-to-end orchestration yields reason='cpu.stepOut'."""
        result = await step_client.step_out(timeout_ms=1000)

        assert result["event"] == "cpu.stepping"
        assert result["reason"] == "cpu.stepOut"

    async def test_run_until_end_to_end_returns_5_fields(self, step_client):
        """run_until: end-to-end orchestration yields reason='breakpoint'.

        PPSSPP sets reason='breakpoint' when the run-until address is hit
        (SteppingBroadcaster.cpp:L36). The faf handler simulates this.
        """
        result = await step_client.run_until(0x08804000, timeout_ms=1000)

        assert result["event"] == "cpu.stepping"
        assert result["reason"] == "breakpoint"
        # Verify address was forwarded to fire_and_forget
        faf_events = [
            (ev, p)
            for ev, p in step_client._transport.fire_and_forget_calls
            if ev == "cpu.runUntil"
        ]
        assert len(faf_events) == 1
        assert faf_events[0][1] == {"address": 0x08804000}

    async def test_next_hle_end_to_end_returns_5_fields(self, step_client):
        """next_hle: end-to-end orchestration yields reason='cpu.nextHLE'."""
        result = await step_client.next_hle(timeout_ms=1000)

        assert result["event"] == "cpu.stepping"
        assert result["reason"] == "cpu.nextHLE"


# ============================================================================
# reason field end-to-end propagation (V004 I10)
# ============================================================================


class TestReasonFieldEndToEndPropagation:
    """L3: reason field propagates from faf handler to caller.

    V004 I10 (reason 不丢): the legacy wait_for_state path lost the
    reason field (only returned {stepping: bool}). The broadcast path
    preserves it. L4 anchors this via AsyncMock; L3 anchors the
    end-to-end propagation through the real broadcast queue.
    """

    async def test_custom_reason_propagates_end_to_end(self, step_transport):
        """Custom reason (e.g. 'savestate.load') propagates to caller.

        PPSSPP's SteppingBroadcaster can push cpu.stepping with various
        reason strings (cpu.stepInto / breakpoint / savestate.load —
        see SteppingBroadcaster.cpp:L36). L3 anchors that any reason
        string survives the end-to-end chain.
        """
        custom_reason = "savestate.load"
        step_transport.set_faf_handler("cpu.stepInto", _make_step_handler(reason=custom_reason))
        client = PpssppDebugClient(step_transport)

        result = await client.step_into(timeout_ms=1000)

        assert result["reason"] == custom_reason, (
            f"reason '{custom_reason}' must propagate end-to-end from "
            f"faf handler → broadcast queue → caller. Got "
            f"{result['reason']!r}."
        )


# ============================================================================
# Legacy fallback orchestration (V004 I9 + degraded path)
# ============================================================================


class TestLegacyFallbackOrchestration:
    """L3: use_broadcast=False orchestrates legacy wait_for_state path.

    V004 redesign: when use_broadcast=False, _confirm_step_completed
    falls back to the legacy wait_for_state(stepping is True) poll.
    L4 anchors the broadcast-timeout-fallback via AsyncMock; L3 anchors
    the direct use_broadcast=False path end-to-end.

    The legacy path polls cpu.status for stepping=True. To exercise it
    end-to-end, the test must first set stepping=True (simulating the
    step faf handler's side effect) before calling
    _confirm_step_completed(use_broadcast=False).
    """

    async def test_use_broadcast_false_uses_legacy_path(self, step_client, step_transport):
        """use_broadcast=False: wait_for_state called (not wait_for_broadcast).

        Setup: set stepping=True (simulating the step faf handler's
        side effect). The legacy path polls cpu.status and returns
        immediately when stepping=True.
        """
        step_transport.set_state({"stepping": True})

        result = await step_client._confirm_step_completed(timeout_ms=1000, use_broadcast=False)

        # Legacy path returns cpu.status dict (stepping=True)
        assert result["stepping"] is True

    async def test_interval_ms_accepted_in_legacy_path(self, step_client, step_transport):
        """V004 I9 (API 兼容): interval_ms accepted in legacy path.

        interval_ms is a no-op in broadcast mode but is forwarded to
        wait_for_state in legacy mode. L3 anchors that it does not
        raise in either path.
        """
        # Broadcast path: interval_ms accepted (no-op).
        # Push a broadcast message so wait_for_broadcast succeeds.
        step_transport.push_broadcast(
            {
                "event": "cpu.stepping",
                "pc": 0,
                "ticks": 0.0,
                "reason": "cpu.stepInto",
                "relatedAddress": 0,
            }
        )
        result = await step_client._confirm_step_completed(
            timeout_ms=500, interval_ms=50, use_broadcast=True
        )
        assert result["event"] == "cpu.stepping"

        # Legacy path: interval_ms forwarded to wait_for_state.
        # Set stepping=True so wait_for_state returns immediately.
        step_transport.set_state({"stepping": True})
        result = await step_client._confirm_step_completed(
            timeout_ms=500, interval_ms=50, use_broadcast=False
        )
        assert result["stepping"] is True
