"""L3 orchestration tests: with_stepping cross-method coordination.

Anchors: B.2 spec §3 V020 invariants I11-I14 under cross-method
orchestration. Complementary to L4 (which anchors isolated V020
invariants in `tests/unit/l4_regression/`).

L3 focus (NOT covered by L4):
- set_reg / evaluate delegate to with_stepping (orchestration)
- Nested with_stepping does NOT re-pause when already stepping
- safe_get_pc + safe_get_threads under shared with_stepping
- Multiple consecutive with_stepping calls each pause/resume
- Exception path leaves CPU in consistent state across orchestration

V020 invariants (B.2 §3.5) anchored here:
- I11: body success + resume failure → propagate (orchestration: set_reg)
- I12: body raised + resume failure → log warning (orchestration: evaluate)
- I13: preserve_state=True + was_stepping=True → no pause/resume
       (orchestration: nested with_stepping)
- I14: pause failure → raise SteppingFailedError, body does NOT run,
       no resume attempted (orchestration: set_reg). The previous
       behavior of swallowing pause failures and running the body
       anyway violated the TrustLevel contract — safe_get_pc /
       safe_get_threads would return HIGH-trust data without the CPU
       actually being paused. The fix raises immediately so callers
       know the stepping context was never entered.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from fake_transport import FakeTransport
from ppsspp_dfx_mcp.core.stepping import SteppingFailedError, SteppingManager, TrustLevel


def _make_regs_response(pc: int = 0x08804000) -> dict[str, Any]:
    """Build a cpu.getAllRegs response with the given PC value."""
    return {
        "categories": [
            {
                "name": "GPR",
                "registerNames": ["r0", "r1", "pc"],
                "uintValues": [0, 0, pc],
            }
        ]
    }


# ============================================================================
# set_reg / evaluate orchestration (V020 I11/I14 under delegation)
# ============================================================================


class TestSetRegOrchestration:
    """L3: set_reg delegates to with_stepping → cpu.setReg.

    Verifies the orchestration: set_reg pauses CPU, sends cpu.setReg,
    resumes CPU. V020 I11/I14 invariants hold under this orchestration
    (L4 anchors the isolated invariants; L3 anchors the delegation).
    """

    async def test_set_reg_pauses_before_setReg(self, client, transport):
        """set_reg orchestrates: pause → cpu.setReg → resume.

        Order matters: cpu.setReg must be sent while CPU is stepping
        (PPSSPP requires CPU paused for setReg to be reliable).
        """
        transport.set_response("cpu.setReg", {"ok": True})

        await client.set_reg("r5", 0x100)

        # Verify fire_and_forget order: cpu.stepping (pause) before cpu.resume
        events = [ev for ev, _ in transport.fire_and_forget_calls]
        stepping_idx = events.index("cpu.stepping")
        resume_idx = events.index("cpu.resume")
        assert stepping_idx < resume_idx, (
            "cpu.stepping (pause) must be sent before cpu.resume — "
            "set_reg orchestration requires pause-before-setReg-resume."
        )
        # Verify cpu.setReg was called (between pause and resume)
        call_events = [ev for ev, _ in transport.calls if ev == "cpu.setReg"]
        assert len(call_events) == 1

    async def test_set_reg_propagates_resume_failure(self, transport):
        """V020 I11 orchestration: set_reg body succeeded + resume fails → propagate.

        L4 anchors I11 on with_stepping directly; L3 anchors that set_reg
        (a with_stepping caller) propagates the resume failure so callers
        know the CPU did NOT return to RUNNING.
        """
        def failing_resume(t: FakeTransport, **params: Any) -> None:
            raise RuntimeError("resume failed")

        transport.set_faf_handler("cpu.resume", failing_resume)
        transport.set_response("cpu.setReg", {"ok": True})
        client = _build_client(transport)

        with pytest.raises(RuntimeError, match="resume failed"):
            await client.set_reg("r5", 0x100)

    async def test_set_reg_skips_resume_on_pause_failure(self, transport):
        """V020 I14 orchestration: set_reg pause fails → raise SteppingFailedError.

        L4 anchors I14 on with_stepping directly; L3 anchors that set_reg
        (a with_stepping caller) propagates the SteppingFailedError raised
        by with_stepping when pause fails. The body (cpu.setReg) must NOT
        run — otherwise HIGH-trust data could be returned without the CPU
        actually being paused (TrustLevel contract violation).

        Verifications:
        - SteppingFailedError is raised (not swallowed).
        - cpu.setReg was NOT sent (body did not execute).
        - cpu.resume was NOT sent (no resume after failed pause).
        """
        def failing_pause(t: FakeTransport, **params: Any) -> None:
            raise RuntimeError("pause failed")

        transport.set_faf_handler("cpu.stepping", failing_pause)
        transport.set_response("cpu.setReg", {"ok": True})
        client = _build_client(transport)

        with pytest.raises(SteppingFailedError, match="pause failed"):
            await client.set_reg("r5", 0x100)

        # Body must NOT have run — cpu.setReg not sent.
        setReg_calls = [ev for ev, _ in transport.calls if ev == "cpu.setReg"]
        assert setReg_calls == [], (
            "set_reg must NOT send cpu.setReg when with_stepping pause "
            "failed (V020 I14 — body must not execute)."
        )
        # Resume must NOT have been attempted.
        events = [ev for ev, _ in transport.fire_and_forget_calls]
        assert "cpu.resume" not in events, (
            "set_reg must NOT resume when its with_stepping pause failed "
            "(V020 I14 orchestration)."
        )


class TestEvaluateOrchestration:
    """L3: evaluate delegates to with_stepping → cpu.evaluate."""

    async def test_evaluate_pauses_resumes_around_call(self, client, transport):
        """evaluate orchestration: pause → cpu.evaluate → resume."""
        transport.set_response("cpu.evaluate", {"value": 42})

        result = await client.evaluate("r5 + 0x10")

        assert result == {"value": 42}
        events = [ev for ev, _ in transport.fire_and_forget_calls]
        assert "cpu.stepping" in events
        assert "cpu.resume" in events
        assert events.index("cpu.stepping") < events.index("cpu.resume")

    async def test_evaluate_logs_warning_on_body_exception_resume_failure(
        self, transport, caplog
    ):
        """V020 I12 orchestration: evaluate body raises + resume fails → log warning.

        L4 anchors I12 on with_stepping directly; L3 anchors that evaluate
        (a with_stepping caller) logs the resume failure as a warning and
        propagates the original body exception unchanged.
        """
        def failing_resume(t: FakeTransport, **params: Any) -> None:
            raise RuntimeError("resume failed")

        transport.set_faf_handler("cpu.resume", failing_resume)
        # Make cpu.evaluate itself raise (simulating body exception)
        transport.set_response(
            "cpu.evaluate", _RaisingResponse(RuntimeError("eval failed"))
        )
        client = _build_client(transport)

        with caplog.at_level(
            logging.WARNING, logger="ppsspp_dfx_mcp.core.stepping"
        ):
            with pytest.raises(RuntimeError, match="eval failed"):
                await client.evaluate("r5")

        warning_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING
            and "with_stepping: resume failed" in r.getMessage()
        ]
        assert len(warning_records) == 1, (
            "evaluate orchestration must log resume failure as warning "
            "when body raised (V020 I12)."
        )


# ============================================================================
# HLE REQUIRED_STEPPING orchestration (batch 1)
# ============================================================================
#
# 6 HLE methods were wrapped with `with_stepping` in batch 1 because their
# PPSSPP contracts gate on Core_IsStepping() (HLESubscriber.cpp):
#   - hle.thread.wake   (ThreadInfoForStatus gate)
#   - hle.thread.stop   (ThreadInfoForStatus gate)
#   - hle.backtrace     (HLESubscriber.cpp:557-559)
#   - hle.func.scan     (HLESubscriber.cpp:486-488)
#   - hle.func.add      (HLESubscriber.cpp:243-245)
#   - hle.func.remove   (HLESubscriber.cpp:322-324)
#
# Each method must orchestrate: pause → call → resume. The pause and resume
# fire_and_forget events must straddle the actual ticketed call.


class TestHleRequiredSteppingOrchestration:
    """L3: REQUIRED_STEPPING methods orchestrate pause → call → resume.

    Parameterized over the 6 methods to verify they all follow the
    with_stepping orchestration pattern. Mirrors TestSetRegOrchestration
    and TestEvaluateOrchestration but covers the batch-1 additions.
    """

    @pytest.mark.parametrize(
        "method_name,call_args,expected_event",
        [
            ("evaluate", {"expression": "pc"}, "cpu.evaluate"),
            ("backtrace", {}, "hle.backtrace"),
            ("func_scan", {"address": 0x08804000, "size": 0x100}, "hle.func.scan"),
            ("func_add", {"name": "f", "address": 0x40}, "hle.func.add"),
            ("func_remove", {"address": 0x40}, "hle.func.remove"),
        ],
        ids=[
            "evaluate",
            "backtrace",
            "func_scan",
            "func_add",
            "func_remove",
        ],
    )
    async def test_method_pauses_before_call_resumes_after(
        self, client, transport, method_name, call_args, expected_event
    ):
        """Orchestration: cpu.stepping (pause) → <event> → cpu.resume.

        Verifies the with_stepping wrap around each HLE method. The
        pause must precede the ticketed call, and the resume must
        follow it. Order is verified via fire_and_forget_calls (pause +
        resume) vs calls (the ticketed event).
        """
        transport.set_response(expected_event, {"ok": True})

        method = getattr(client, method_name)
        await method(**call_args)

        # Verify pause + resume fire_and_forget events straddle the call.
        faf_events = [ev for ev, _ in transport.fire_and_forget_calls]
        assert "cpu.stepping" in faf_events, (
            f"{method_name} must pause CPU via cpu.stepping fire_and_forget "
            f"before issuing {expected_event}."
        )
        assert "cpu.resume" in faf_events, (
            f"{method_name} must resume CPU via cpu.resume fire_and_forget "
            f"after issuing {expected_event}."
        )
        assert faf_events.index("cpu.stepping") < faf_events.index("cpu.resume"), (
            f"{method_name} orchestration: cpu.stepping must precede cpu.resume."
        )
        # Verify the ticketed event was issued exactly once.
        ticketed = [ev for ev, _ in transport.calls if ev == expected_event]
        assert len(ticketed) == 1, (
            f"{method_name} must issue {expected_event} exactly once."
        )

    @pytest.mark.parametrize(
        "method_name,call_args",
        [
            ("evaluate", {"expression": "pc"}),
            ("backtrace", {}),
            ("func_scan", {"address": 0x08804000, "size": 0x100}),
            ("func_add", {"name": "f", "address": 0x40}),
            ("func_remove", {"address": 0x40}),
        ],
        ids=[
            "evaluate",
            "backtrace",
            "func_scan",
            "func_add",
            "func_remove",
        ],
    )
    async def test_method_skips_pause_when_already_stepping(
        self, client, transport, method_name, call_args
    ):
        """Orchestration: when CPU already stepping, no pause/resume issued.

        V020 I13: preserve_state=True (default) + was_stepping=True →
        no pause/resume. Pre-set stepping=True and verify the method
        only issues the ticketed call (no cpu.stepping / cpu.resume
        fire_and_forget events).
        """
        transport.set_state({"stepping": True})

        method = getattr(client, method_name)
        # Set a generic response so the call doesn't fail.
        # We don't care about the event name here — just the faf calls.
        transport.set_response("hle.thread.wake", {"ok": True})
        transport.set_response("hle.thread.stop", {"ok": True})
        transport.set_response("hle.backtrace", {"ok": True})
        transport.set_response("hle.func.scan", {"ok": True})
        transport.set_response("hle.func.add", {"ok": True})
        transport.set_response("hle.func.remove", {"ok": True})

        await method(**call_args)

        faf_events = [ev for ev, _ in transport.fire_and_forget_calls]
        assert "cpu.stepping" not in faf_events, (
            f"{method_name} must NOT send cpu.stepping when CPU already "
            f"stepping (V020 I13 orchestration, preserve_state=True)."
        )
        assert "cpu.resume" not in faf_events, (
            f"{method_name} must NOT send cpu.resume when CPU already "
            f"stepping (V020 I13 orchestration, preserve_state=True)."
        )


# ============================================================================
# Nested with_stepping (V020 I13 orchestration)
# ============================================================================


class TestNestedWithStepping:
    """L3: nested with_stepping does NOT re-pause when already stepping.

    V020 I13 (B.2 §3.5): preserve_state=True + was_stepping=True →
    should_resume_on_exit=False. L4 anchors this on a single
    with_stepping call; L3 anchors that the inner with_stepping
    (called while the outer already paused) does NOT send a second
    cpu.stepping pause event.
    """

    async def test_inner_with_stepping_no_second_pause(self, manager, transport):
        """Inner with_stepping preserves outer's stepping state.

        Outer with_stepping pauses CPU (stepping=True). Inner
        with_stepping(preserve_state=True) sees was_stepping=True →
        does NOT send cpu.stepping again, does NOT resume on exit.
        """
        async with manager.with_stepping():
            # CPU now paused
            assert transport.state.get("stepping") is True
            pause_count_before = sum(
                1 for ev, _ in transport.fire_and_forget_calls
                if ev == "cpu.stepping"
            )

            async with manager.with_stepping(preserve_state=True):
                # Still paused; no additional cpu.stepping sent
                pause_count_after = sum(
                    1 for ev, _ in transport.fire_and_forget_calls
                    if ev == "cpu.stepping"
                )
                assert pause_count_after == pause_count_before, (
                    "Inner with_stepping must NOT send cpu.stepping when "
                    "outer already paused (V020 I13 orchestration)."
                )

            # Inner exited: CPU should still be paused (preserve_state=True)
            assert transport.state.get("stepping") is True

        # Outer exited: CPU resumed
        assert transport.state.get("stepping") is False

    async def test_consecutive_with_stepping_each_pauses_resumes(
        self, manager, transport
    ):
        """Two consecutive with_stepping calls each pause and resume.

        Unlike nested calls (which share the paused state), consecutive
        calls must each perform their own pause/resume cycle.
        """
        for _ in range(3):
            async with manager.with_stepping():
                assert transport.state.get("stepping") is True
            assert transport.state.get("stepping") is False

        # Verify 3 pause + 3 resume events
        events = [ev for ev, _ in transport.fire_and_forget_calls]
        assert events.count("cpu.stepping") == 3
        assert events.count("cpu.resume") == 3


# ============================================================================
# safe_get_pc + safe_get_threads orchestration (shared with_stepping)
# ============================================================================


class TestSafeQueryOrchestration:
    """L3: safe_get_pc / safe_get_threads orchestrate with_stepping.

    Each safe query method independently uses with_stepping (they do
    NOT share a single pause). L3 anchors that consecutive safe queries
    each perform their own pause/resume cycle.
    """

    async def test_safe_get_pc_then_safe_get_threads_each_pause(
        self, manager, transport
    ):
        """Consecutive safe_get_pc + safe_get_threads each pause/resume.

        Two safe queries → two pause/resume cycles (NOT shared).
        """
        transport.set_response(
            "cpu.getAllRegs", _make_regs_response(pc=0x088E0D5C)
        )
        transport.set_response("hle.thread.list", {"threads": []})

        pc, trust = await manager.safe_get_pc()
        assert pc == 0x088E0D5C
        assert trust == TrustLevel.HIGH

        snap = await manager.safe_get_threads()
        assert snap.trust_level == TrustLevel.HIGH

        # Two pause + two resume cycles
        events = [ev for ev, _ in transport.fire_and_forget_calls]
        assert events.count("cpu.stepping") == 2
        assert events.count("cpu.resume") == 2

    async def test_safe_get_pc_returns_high_trust_under_orchestration(
        self, manager, transport
    ):
        """safe_get_pc orchestration yields TrustLevel.HIGH.

        V026 (B.2 §5.2): TrustLevel.from_pc_result currently returns
        HIGH unconditionally because safe_get_pc always uses
        with_stepping. L3 anchors that the orchestration
        (with_stepping → cpu.getAllRegs → _extract_pc →
        TrustLevel.from_pc_result) yields HIGH.
        """
        transport.set_response(
            "cpu.getAllRegs", _make_regs_response(pc=0x12345678)
        )

        pc, trust = await manager.safe_get_pc()

        assert pc == 0x12345678
        assert trust == TrustLevel.HIGH


# ============================================================================
# Helpers
# ============================================================================


class _RaisingResponse:
    """A callable response that raises when invoked (for FakeTransport)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __call__(self, **params: Any) -> dict[str, Any]:
        raise self._exc


def _build_client(transport: FakeTransport):
    """Build a PpssppDebugClient with short timeout for fast tests."""
    from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient
    # Patch SteppingManager defaults via direct construction
    client = PpssppDebugClient(transport)
    # Override the stepping manager with shorter timeouts
    client._stepping = SteppingManager(
        transport, default_timeout_ms=500, default_interval_ms=5
    )
    return client
