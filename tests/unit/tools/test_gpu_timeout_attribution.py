"""Attribution must name WHY a ticketed call hung, not just that it did.

D16 was misdiagnosed twice because the only signal available was "timed
out", which is compatible with four different situations. This pins the
distinction (C4.4):

  expected_stall -- the CPU was stepping; waiting was correct
  pairing_broken -- a same-event broadcast arrived meanwhile; ticket
                    pairing is what failed
  no_producer    -- nothing arrived at all. D16's real cause: a modal
                    "Graphics Error" dialog blocked the emulator, so no GPU
                    flip ever happened
  transport_error -- a non-timeout failure

Each case must be distinguishable from the record fields alone, and each
must carry a hint that names the NEXT ACTION rather than restating the
symptom.
"""

from __future__ import annotations

from ppsspp_dfx_mcp.core.call_attribution import (
    CAUSE_EXPECTED_STALL,
    CAUSE_NO_PRODUCER,
    CAUSE_PAIRING_BROKEN,
    CAUSE_TRANSPORT_ERROR,
    CAUSE_UNKNOWN,
    attribute_timeout,
    describe,
    hint_for,
)
from ppsspp_dfx_mcp.core.call_diagnostics import CallDiagnostics


def _rec(**kw) -> dict:
    base = {
        "event": "gpu.stats.get",
        "ticket": "t-1",
        "issued_at": 0.0,
        "elapsed_s": 5.0,
        "settled": True,
        "timed_out": True,
        "error": "handshake timeout",
        "stepping_at_timeout": False,
        "last_same_event_broadcast_at": None,
    }
    base.update(kw)
    return base


class TestTheFourCauses:
    def test_stepping_is_an_expected_stall(self) -> None:
        """Waiting for a frame that will never come is correct behaviour."""
        assert attribute_timeout(_rec(stepping_at_timeout=True)) == CAUSE_EXPECTED_STALL

    def test_stepping_wins_even_if_other_fields_set(self) -> None:
        """If the CPU was stepping, that is the answer regardless."""
        got = attribute_timeout(_rec(stepping_at_timeout=True, last_same_event_broadcast_at=1.0))
        assert got == CAUSE_EXPECTED_STALL

    def test_broadcast_meanwhile_means_pairing_broke(self) -> None:
        got = attribute_timeout(_rec(last_same_event_broadcast_at=123.0))
        assert got == CAUSE_PAIRING_BROKEN

    def test_silence_means_no_producer(self) -> None:
        """D16's signature: a timeout with no broadcast whatsoever."""
        assert attribute_timeout(_rec()) == CAUSE_NO_PRODUCER

    def test_closed_transport_is_not_attributed_to_the_producer(self) -> None:
        """A dead transport must not be reported as "nothing produced"."""
        got = attribute_timeout(_rec(error="connection closed"))
        assert got == CAUSE_TRANSPORT_ERROR


class TestRobustness:
    def test_missing_record_is_unknown(self) -> None:
        assert attribute_timeout(None) == CAUSE_UNKNOWN

    def test_empty_record_is_unknown(self) -> None:
        assert attribute_timeout({}) == CAUSE_UNKNOWN

    def test_non_timeout_record_is_unknown(self) -> None:
        """A call that succeeded has nothing to attribute."""
        got = attribute_timeout(_rec(timed_out=False, error=None, settled=True))
        assert got == CAUSE_UNKNOWN

    def test_unknown_record_shape_does_not_raise(self) -> None:
        """A malformed record must not mask the original timeout."""
        assert attribute_timeout({"timed_out": "yes", "stepping_at_timeout": "maybe"})

    def test_junk_error_type_does_not_raise(self) -> None:
        assert attribute_timeout(_rec(error=object()))  # type: ignore[arg-type]


class TestHintsAreActionable:
    """A hint that restates the symptom is not a hint."""

    def test_every_cause_has_a_non_empty_hint(self) -> None:
        for cause in (
            CAUSE_EXPECTED_STALL,
            CAUSE_PAIRING_BROKEN,
            CAUSE_NO_PRODUCER,
            CAUSE_TRANSPORT_ERROR,
            CAUSE_UNKNOWN,
        ):
            assert hint_for(cause), f"{cause} has no hint"

    def test_no_producer_hint_does_not_send_you_chasing_rendering(self) -> None:
        """Revised 2026-10-02 after the T036 experiment.

        The hint USED to say "the emulator is blocked before it can render --
        check for a modal dialog". That was D16's cause and it was fixed by
        pinning the graphics backend. On a live session the same timeout then
        coexisted with a 59.4 fps render and an animated screen, so the old
        hint sent the agent chasing a problem that was already fixed, and away
        from the real one (this event has no producer even though frames are
        produced). The new contract: the hint must state the distinction
        explicitly and point at the measurement.
        """
        hint = hint_for(CAUSE_NO_PRODUCER)
        lowered = hint.lower()
        assert "not the same as" in lowered and "render" in lowered, (
            "the hint must explicitly separate 'no producer for this event' "
            "from 'not rendering' -- conflating them is what made D16 hard"
        )
        assert "gpu_channel_root_cause" in lowered, (
            "the hint must point at the measurement that justified the wording"
        )
        assert "modal dialog" not in lowered, (
            "the dialog is no longer the operative cause (the backend pin "
            "fixed it); naming it first sends the reader to a fixed problem"
        )

    def test_expected_stall_hint_tells_you_to_resume(self) -> None:
        hint = hint_for(CAUSE_EXPECTED_STALL)
        assert "stepping" in hint.lower() or "resume" in hint.lower()

    def test_unknown_hint_is_safe(self) -> None:
        assert hint_for("not-a-real-cause") == hint_for(CAUSE_UNKNOWN)


class TestDescribe:
    def test_describe_echoes_the_evidence(self) -> None:
        """A reader must be able to check the reasoning, not trust it."""
        out = describe(_rec())
        assert out["cause"] == CAUSE_NO_PRODUCER
        ev = out["evidence"]
        assert ev["timed_out"] is True
        assert ev["saw_same_event_broadcast"] is False
        assert ev["stepping_at_timeout"] is False
        assert ev["elapsed_s"] == 5.0
        assert ev["error"] == "handshake timeout"

    def test_describe_handles_none(self) -> None:
        out = describe(None)
        assert out["cause"] == CAUSE_UNKNOWN
        assert out["evidence"]["timed_out"] is None


class TestAgainstRealDiagnostics:
    """End to end: feed the ring buffer, read the attribution."""

    def test_d16_signature_is_attributed_to_no_producer(self) -> None:
        d = CallDiagnostics()
        rec = d.begin("gpu.stats.get", "t-1")
        d.settle(rec, timed_out=True, error="version handshake timeout", stepping=False)
        out = describe(d.last())
        assert out["cause"] == CAUSE_NO_PRODUCER
        assert "not the same as" in out["hint"].lower()

    def test_pairing_signature_is_distinguished(self) -> None:
        d = CallDiagnostics()
        rec = d.begin("gpu.stats.get", "t-1")
        d.note_broadcast("gpu.stats.get")
        d.settle(rec, timed_out=True, error="no reply", stepping=False)
        assert describe(d.last())["cause"] == CAUSE_PAIRING_BROKEN

    def test_stepping_signature_is_distinguished(self) -> None:
        d = CallDiagnostics()
        rec = d.begin("gpu.stats.get", "t-1")
        d.settle(rec, timed_out=True, error="no reply", stepping=True)
        assert describe(d.last())["cause"] == CAUSE_EXPECTED_STALL


class TestFreezeSuspectedPathIsAttributed:
    """Finding H5 (measured 2026-10-02): the attribution must also run when the
    timeout arrives as CPU_FREEZE_SUSPECTED, not as WsTimeout.

    A real gpu.stats timeout on a live session is re-classified by
    ``to_tool_error`` as ``CpuFreezeSuspected`` (PID alive AND game running),
    so it never matched the ``except WsTimeout`` arm. The three-way attribution
    therefore did NOT run on the one failure it exists to explain, and the tool
    reported "CPU freeze suspected" while a screenshot from the same second
    showed a 59.4 fps render.

    Reproduce: mcp_test_report/gpu_channel_root_cause.md section 3, H5.
    """

    def test_freeze_suspected_timeout_carries_the_cause_and_keeps_its_code(self) -> None:
        import asyncio

        import pytest

        import ppsspp_dfx_mcp.tools.gpu_stats as gs
        from ppsspp_dfx_mcp.core.call_diagnostics import CallDiagnostics
        from ppsspp_dfx_mcp.errors import CpuFreezeSuspected, ToolError

        # A record with nothing broadcast at all -> no_producer
        diag = CallDiagnostics()
        rec = diag.begin("gpu.stats.get", "t-1")
        diag.settle(rec, timed_out=True, error="no reply", stepping=False)

        class _FakeClient:
            def __init__(self) -> None:
                self._transport = type("T", (), {"diagnostics": diag})()

            async def gpu_stats(self):
                raise CpuFreezeSuspected("gpu.stats.get timed out; PID alive; game running")

        class _CM:
            async def __aenter__(self):
                return _FakeClient()

            async def __aexit__(self, *exc):
                return False

        orig = gs.session_client
        gs.session_client = lambda sid: _CM()
        try:
            with pytest.raises(ToolError) as ei:
                asyncio.run(gs.gpu_stats(session_id="s"))
        finally:
            gs.session_client = orig

        msg = str(ei.value)
        assert "cause: no_producer" in msg, (
            f"the attribution did not run on the freeze-suspected path: {msg!r}"
        )
        assert "next:" in msg, "the hint (what to do next) is missing"
        # The error class must be preserved -- this is still a freeze suspicion.
        assert ei.value.code == "CPU_FREEZE_SUSPECTED"
        # And the code prefix must appear exactly once (str() adds it).
        assert msg.count("[CPU_FREEZE_SUSPECTED]") == 1, f"double-prefixed: {msg!r}"

    def test_other_tool_errors_are_not_rewritten(self) -> None:
        """The freeze-suspected condition must be load-bearing.

        Without this, the fix could be widened to every ToolError and the test
        above would still pass -- while silently rewriting unrelated failures
        (e.g. a bad argument) with a GPU hint.
        """
        import asyncio

        import pytest

        import ppsspp_dfx_mcp.tools.gpu_stats as gs
        from ppsspp_dfx_mcp.core.call_diagnostics import CallDiagnostics
        from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError

        diag = CallDiagnostics()
        rec = diag.begin("gpu.stats.get", "t-1")
        diag.settle(rec, timed_out=True, error="no reply", stepping=False)

        class _FakeClient:
            def __init__(self) -> None:
                self._transport = type("T", (), {"diagnostics": diag})()

            async def gpu_stats(self):
                raise ArgsInvalid("session_id must not be blank")

        class _CM:
            async def __aenter__(self):
                return _FakeClient()

            async def __aexit__(self, *exc):
                return False

        orig = gs.session_client
        gs.session_client = lambda sid: _CM()
        try:
            with pytest.raises(ToolError) as ei:
                asyncio.run(gs.gpu_stats(session_id="s"))
        finally:
            gs.session_client = orig

        msg = str(ei.value)
        assert "cause:" not in msg, f"an unrelated ToolError was rewritten: {msg!r}"
        assert ei.value.code == "ARGS_INVALID"
