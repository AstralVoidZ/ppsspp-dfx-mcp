"""CallDiagnostics: the six attribution fields D16 root-cause work needs.

T007 delivered the ring buffer; T038 pins the contract C4.3 / SC-014 that
the attribution logic (T039) will read. The fields exist so a human can
tell these three causes apart when a ticketed call hangs:

  timed_out / error          -- the call itself failed
  stepping_at_timeout        -- was the CPU stepping (an expected stall)
  last_same_event_broadcast_at
                             -- did a broadcast for the SAME event arrive
                                meanwhile? If yes the response path is alive
                                and ticket pairing is what broke. If no,
                                nothing is producing the event at all.

That distinction is what makes D16 diagnosable instead of guessable, and
D16 proved it the hard way: the real cause was a modal dialog blocking the
emulator, which is only visible as "no broadcast ever arrived".
"""

from __future__ import annotations

import time

import pytest

from ppsspp_dfx_mcp.core.call_diagnostics import CallDiagnostics, CallRecord


class TestRecordFields:
    """C4.3 -- the six attribution fields."""

    def test_new_record_is_pending_with_no_outcome(self) -> None:
        d = CallDiagnostics()
        rec = d.begin("gpu.stats.get", "t-1")
        assert rec.event == "gpu.stats.get"
        assert rec.ticket == "t-1"
        assert rec.settled is False
        assert rec.timed_out is False
        assert rec.error is None
        assert rec.stepping_at_timeout is None
        assert rec.last_same_event_broadcast_at is None
        assert rec.elapsed_s is None

    def test_settle_records_elapsed_and_outcome(self) -> None:
        d = CallDiagnostics()
        rec = d.begin("gpu.stats.get", "t-1")
        time.sleep(0.01)
        d.settle(rec, timed_out=True, error="handshake timeout", stepping=False)
        assert rec.settled is True
        assert rec.timed_out is True
        assert rec.error == "handshake timeout"
        assert rec.stepping_at_timeout is False
        assert rec.elapsed_s is not None and rec.elapsed_s > 0

    def test_to_dict_exposes_every_attribution_field(self) -> None:
        d = CallDiagnostics()
        rec = d.begin("gpu.stats.get", "t-1")
        d.settle(rec, timed_out=True, error="boom", stepping=True)
        d.note_broadcast("gpu.stats.get")
        row = d.last()
        assert row is not None
        for key in (
            "event",
            "ticket",
            "issued_at",
            "elapsed_s",
            "settled",
            "timed_out",
            "error",
            "stepping_at_timeout",
            "last_same_event_broadcast_at",
        ):
            assert key in row, f"attribution field {key!r} is missing from to_dict()"

    def test_to_dict_is_json_serialisable(self) -> None:
        """Records get embedded in tool output, so they must not carry
        non-serialisable objects."""
        import json

        d = CallDiagnostics()
        rec = d.begin("gpu.stats.get", "t-1")
        d.settle(rec, timed_out=True, error="x", stepping=None)

        # Values survive the round-trip -- a to_dict() that dropped the
        # attribution fields would still "not raise", so pin the content.
        restored = json.loads(json.dumps(d.recent()))
        assert [r["ticket"] for r in restored] == ["t-1"]
        assert restored[0]["event"] == "gpu.stats.get"
        assert restored[0]["timed_out"] is True
        assert restored[0]["error"] == "x"

    def test_unknown_stepping_is_none_not_false(self) -> None:
        """None means 'could not tell'; False would read as 'running'."""
        d = CallDiagnostics()
        rec = d.begin("gpu.stats.get", "t-1")
        d.settle(rec, timed_out=True, stepping=None)
        assert rec.stepping_at_timeout is None

    def test_error_outcome_does_not_imply_timeout(self) -> None:
        """A transport error and a timeout are independent outcomes."""
        d = CallDiagnostics()
        rec = d.begin("cpu.evaluate", "t-1")
        d.settle(rec, error="RuntimeError: boom")
        assert rec.timed_out is False
        assert "boom" in (rec.error or "")


class TestBroadcastAttribution:
    """The field that distinguishes "no producer" from "pairing broke"."""

    def test_broadcast_stamps_pending_calls_of_the_same_event(self) -> None:
        d = CallDiagnostics()
        rec = d.begin("gpu.stats.get", "t-1")
        assert rec.last_same_event_broadcast_at is None
        d.note_broadcast("gpu.stats.get")
        assert rec.last_same_event_broadcast_at is not None

    def test_broadcast_ignores_other_events(self) -> None:
        d = CallDiagnostics()
        rec = d.begin("gpu.stats.get", "t-1")
        d.note_broadcast("something.else")
        assert rec.last_same_event_broadcast_at is None, (
            "a broadcast for a different event must not be credited to this "
            "call -- that would mask exactly the case D16 hit"
        )

    def test_settled_calls_are_not_stamped(self) -> None:
        d = CallDiagnostics()
        rec = d.begin("gpu.stats.get", "t-1")
        d.settle(rec, timed_out=True)
        d.note_broadcast("gpu.stats.get")
        assert rec.last_same_event_broadcast_at is None, (
            "a call that already finished must not be retro-stamped"
        )

    def test_no_broadcast_leaves_the_field_none(self) -> None:
        """This is the D16 signature: a timeout with no broadcast at all."""
        d = CallDiagnostics()
        rec = d.begin("gpu.stats.get", "t-1")
        d.settle(rec, timed_out=True, error="timeout", stepping=False)
        row = d.last()
        assert row is not None
        assert row["timed_out"] is True
        assert row["last_same_event_broadcast_at"] is None


class TestQueries:
    def test_timeouts_filters_to_failures_only(self) -> None:
        d = CallDiagnostics()
        ok = d.begin("read_memory", "t-1")
        d.settle(ok)
        bad = d.begin("gpu.stats.get", "t-2")
        d.settle(bad, timed_out=True, error="timeout")
        assert [r["ticket"] for r in d.timeouts()] == ["t-2"]

    def test_event_filter_narrows_results(self) -> None:
        d = CallDiagnostics()
        a = d.begin("gpu.stats.get", "t-1")
        b = d.begin("read_memory", "t-2")
        d.settle(a, timed_out=True)
        d.settle(b)
        assert [r["ticket"] for r in d.recent("gpu.stats.get")] == ["t-1"]
        assert len(d.recent()) == 2

    def test_last_returns_newest(self) -> None:
        d = CallDiagnostics()
        for t in ("t-1", "t-2", "t-3"):
            d.settle(d.begin("e", t))
        assert d.last()["ticket"] == "t-3"

    def test_empty_queries_return_empty_not_none(self) -> None:
        d = CallDiagnostics()
        assert d.recent() == []
        assert d.timeouts() == []
        assert d.last() is None
        assert d.last("never.happened") is None

    def test_limit_keeps_the_newest(self) -> None:
        d = CallDiagnostics()
        for i in range(5):
            d.settle(d.begin("e", f"t-{i}"))
        assert [r["ticket"] for r in d.recent(limit=2)] == ["t-3", "t-4"]


class TestRingBuffer:
    def test_capacity_is_enforced_oldest_dropped(self) -> None:
        d = CallDiagnostics(capacity=3)
        for i in range(6):
            d.settle(d.begin("e", f"t-{i}"))
        rows = d.recent()
        assert len(rows) == 3, "the ring buffer must not grow without bound"
        assert [r["ticket"] for r in rows] == ["t-3", "t-4", "t-5"]

    def test_capacity_below_one_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="capacity"):
            CallDiagnostics(capacity=0)

    def test_clear_empties(self) -> None:
        d = CallDiagnostics()
        d.settle(d.begin("e", "t-1"))
        d.clear()
        assert len(d) == 0
        assert d.recent() == []


class TestAttributionScenario:
    """The exact distinction D16 needed, end to end on the buffer."""

    def test_pairing_failure_is_distinguishable_from_no_producer(self) -> None:
        d = CallDiagnostics()

        # A: broadcast arrived meanwhile -> response path alive, pairing broke
        a = d.begin("gpu.stats.get", "t-A")
        d.note_broadcast("gpu.stats.get")
        d.settle(a, timed_out=True, error="no reply", stepping=False)

        # B: nothing arrived -> the producer is not running at all
        b = d.begin("gpu.stats.get", "t-B")
        d.settle(b, timed_out=True, error="no reply", stepping=False)

        ra, rb = d.recent()
        assert ra["last_same_event_broadcast_at"] is not None
        assert rb["last_same_event_broadcast_at"] is None
        assert ra["timed_out"] == rb["timed_out"] is True
        # identical symptoms upstream, distinguishable here

    def test_stepping_is_recorded_separately(self) -> None:
        """A stall while stepping is expected; a stall while paused is not."""
        d = CallDiagnostics()
        a = d.begin("gpu.stats.get", "t-1")
        d.settle(a, timed_out=True, stepping=True)
        b = d.begin("gpu.stats.get", "t-2")
        d.settle(b, timed_out=True, stepping=False)
        rows = d.recent()
        assert rows[0]["stepping_at_timeout"] is True
        assert rows[1]["stepping_at_timeout"] is False


class TestRecordType:
    def test_call_record_defaults_are_independent(self) -> None:
        """A shared mutable default would leak state between records."""
        a = CallRecord(event="e", ticket="t-1", issued_at=time.monotonic())
        b = CallRecord(event="e", ticket="t-2", issued_at=time.monotonic())
        a.extra["k"] = 1
        assert b.extra == {}, "extra dict is shared between records"

    def test_elapsed_is_rounded(self) -> None:
        d = CallDiagnostics()
        rec = d.begin("e", "t-1")
        d.settle(rec)
        assert rec.elapsed_s == round(rec.elapsed_s, 6)
