"""Tests for ReplayTransport (replay_transport.py).

Verifies that ReplayTransport:
- Loads records from a cassette file (from_cassette)
- Replays call records with time-agnostic matching (by order + event)
- Best-effort tolerance: param mismatch falls back to event match
- Replays fire_and_forget with state MERGE semantics (not replacement)
- Pre-enqueues broadcasts on init; wait_for_broadcast consumes FIFO
- cpu.status returns the current state (mirrors FakeTransport)
- Raises CassetteExhaustedError when no matching record remains
- Implements FakeTransport-compatible interface (set_state / state / etc.)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from record_replay.cassette import CassetteRecord, save_cassette
from record_replay.replay_transport import (
    CassetteExhaustedError,
    ReplayTransport,
)


# ---------- Helper: build a cassette from records and load it ----------


def _build_replay(records: list[CassetteRecord]) -> ReplayTransport:
    """Build a ReplayTransport directly from a list of records."""
    return ReplayTransport(records)


def _build_cassette_and_replay(records: list[CassetteRecord], tmp_path: Path) -> ReplayTransport:
    """Build a ReplayTransport via a JSONL cassette file (end-to-end)."""
    cassette_path = tmp_path / "cassette.jsonl"
    save_cassette(records, cassette_path)
    return ReplayTransport.from_cassette(cassette_path)


# ---------- call() replay tests ----------


class TestReplayCall:
    """ReplayTransport.call replays the next matching call record."""

    @pytest.mark.asyncio
    async def test_call_replays_exact_event_match(self):
        records = [
            CassetteRecord(
                type="call",
                event="memory.read_u32",
                params={"address": 0x08804000},
                response={"value": 0xDEADBEEF},
                timestamp=1.0,
            ),
        ]
        replay = _build_replay(records)

        result = await replay.call("memory.read_u32", address=0x08804000)

        assert result == {"value": 0xDEADBEEF}

    @pytest.mark.asyncio
    async def test_call_replays_in_recorded_order(self):
        """Multiple calls replay in the order they were recorded."""
        records = [
            CassetteRecord(
                type="call", event="memory.read_u32",
                params={"address": 0x08804000},
                response={"value": 1}, timestamp=1.0,
            ),
            CassetteRecord(
                type="call", event="memory.read_u32",
                params={"address": 0x08804004},
                response={"value": 2}, timestamp=2.0,
            ),
        ]
        replay = _build_replay(records)

        r1 = await replay.call("memory.read_u32", address=0x08804000)
        r2 = await replay.call("memory.read_u32", address=0x08804004)

        assert r1 == {"value": 1}
        assert r2 == {"value": 2}

    @pytest.mark.asyncio
    async def test_call_best_effort_param_mismatch(self):
        """Best-effort: param mismatch falls back to event match.

        If the recorded params don't exactly match the call params,
        ReplayTransport still returns the first record with matching
        event (best-effort tolerance, avoids brittle replay).
        """
        records = [
            CassetteRecord(
                type="call", event="memory.read_u32",
                params={"address": 0x08804000},  # recorded address
                response={"value": 0xDEADBEEF},
                timestamp=1.0,
            ),
        ]
        replay = _build_replay(records)

        # Call with a different address — best-effort still returns.
        result = await replay.call("memory.read_u32", address=0x99999999)
        assert result == {"value": 0xDEADBEEF}

    @pytest.mark.asyncio
    async def test_call_raises_cassette_exhausted_when_no_match(self):
        """When no call record matches the event, CassetteExhaustedError is raised."""
        records = [
            CassetteRecord(
                type="call", event="memory.read_u32",
                params={}, response={"value": 1}, timestamp=1.0,
            ),
        ]
        replay = _build_replay(records)

        # Different event — no match.
        with pytest.raises(CassetteExhaustedError):
            await replay.call("memory.read_bytes", address=0x08804000, length=16)

    @pytest.mark.asyncio
    async def test_cpu_status_returns_current_state(self):
        """cpu.status event returns the current state (mirrors FakeTransport)."""
        records: list[CassetteRecord] = []
        replay = _build_replay(records)
        replay.set_state({"stepping": True, "pc": 0x08804000})

        result = await replay.call("cpu.status")

        assert result == {"stepping": True, "pc": 0x08804000}


# ---------- fire_and_forget() state merge tests ----------


class TestReplayFireAndForgetStateMerge:
    """fire_and_forget replays state changes with MERGE semantics."""

    @pytest.mark.asyncio
    async def test_state_delta_merged_not_replaced(self):
        """State delta is MERGED into current state, not replacing it.

        This is the critical spec requirement (Decision 3 in design.md):
        ReplayTransport must merge state_delta as {**cur, **delta}, so
        other state fields (ticks, pc) are preserved during replay.
        """
        records = [
            # Initial state will have stepping=False, ticks=100
            # (seeded via set_state below).
            CassetteRecord(
                type="fire_and_forget",
                event="cpu.stepping",
                params={"step": "into"},
                timestamp=1.0,
            ),
            CassetteRecord(
                type="state_change",
                event="cpu.stepping",
                state_delta={"stepping": True},
                timestamp=1.1,
            ),
        ]
        replay = _build_replay(records)
        # Seed initial state with extra fields that should be PRESERVED.
        replay.set_state({"stepping": False, "ticks": 100, "pc": 0x08804000})

        await replay.fire_and_forget("cpu.stepping", step="into")

        state = replay.state
        # stepping was merged to True.
        assert state["stepping"] is True
        # ticks and pc must be PRESERVED (merge, not replace).
        assert state["ticks"] == 100
        assert state["pc"] == 0x08804000

    @pytest.mark.asyncio
    async def test_multiple_state_changes_accumulate(self):
        """Multiple fire_and_forget state changes accumulate via merge."""
        records = [
            CassetteRecord(type="fire_and_forget", event="cpu.stepping",
                           params={"step": "into"}, timestamp=1.0),
            CassetteRecord(type="state_change", event="cpu.stepping",
                           state_delta={"stepping": True}, timestamp=1.1),
            CassetteRecord(type="fire_and_forget", event="cpu.stepping",
                           params={"step": "resume"}, timestamp=2.0),
            CassetteRecord(type="state_change", event="cpu.stepping",
                           state_delta={"stepping": False}, timestamp=2.1),
        ]
        replay = _build_replay(records)
        replay.set_state({"stepping": False, "pc": 0x08804000})

        await replay.fire_and_forget("cpu.stepping", step="into")
        assert replay.state["stepping"] is True
        assert replay.state["pc"] == 0x08804000

        await replay.fire_and_forget("cpu.stepping", step="resume")
        assert replay.state["stepping"] is False
        assert replay.state["pc"] == 0x08804000

    @pytest.mark.asyncio
    async def test_no_state_change_record_leaves_state_untouched(self):
        """If there's no paired state_change record, state is untouched."""
        records = [
            CassetteRecord(type="fire_and_forget", event="cpu.stepping",
                           params={"step": "into"}, timestamp=1.0),
            # NO state_change record paired.
        ]
        replay = _build_replay(records)
        replay.set_state({"stepping": False, "pc": 0x08804000})

        await replay.fire_and_forget("cpu.stepping", step="into")

        assert replay.state["stepping"] is False  # unchanged
        assert replay.state["pc"] == 0x08804000

    @pytest.mark.asyncio
    async def test_faf_raises_cassette_exhausted_when_empty(self):
        records: list[CassetteRecord] = []
        replay = _build_replay(records)

        with pytest.raises(CassetteExhaustedError):
            await replay.fire_and_forget("cpu.stepping", step="into")


# ---------- wait_for_broadcast() replay tests ----------


class TestReplayBroadcast:
    """Broadcasts are pre-enqueued on init; wait_for_broadcast consumes FIFO."""

    @pytest.mark.asyncio
    async def test_broadcast_pre_enqueued_and_consumed(self):
        """Broadcast records are pre-enqueued on init."""
        broadcast_msg = {"event": "cpu.stepping", "stepping": True}
        records = [
            CassetteRecord(
                type="broadcast", event="cpu.stepping",
                message=broadcast_msg, timestamp=1.0,
            ),
        ]
        replay = _build_replay(records)

        msg = await replay.wait_for_broadcast("cpu.stepping", timeout_ms=1000)

        assert msg == broadcast_msg

    @pytest.mark.asyncio
    async def test_non_matching_broadcast_requeued(self):
        """Non-matching broadcasts are requeued for other consumers."""
        records = [
            CassetteRecord(
                type="broadcast", event="other.event",
                message={"event": "other.event"}, timestamp=1.0,
            ),
            CassetteRecord(
                type="broadcast", event="cpu.stepping",
                message={"event": "cpu.stepping", "stepping": True},
                timestamp=2.0,
            ),
        ]
        replay = _build_replay(records)

        # First wait gets cpu.stepping (the second broadcast).
        msg = await replay.wait_for_broadcast("cpu.stepping", timeout_ms=1000)
        assert msg["event"] == "cpu.stepping"

        # The non-matching broadcast should still be in the queue.
        other = await replay.wait_for_broadcast("other.event", timeout_ms=1000)
        assert other["event"] == "other.event"

    @pytest.mark.asyncio
    async def test_wait_for_broadcast_timeout_raises(self):
        """No matching broadcast within timeout raises TimeoutError."""
        records: list[CassetteRecord] = []  # no broadcasts
        replay = _build_replay(records)

        with pytest.raises(TimeoutError):
            await replay.wait_for_broadcast("cpu.stepping", timeout_ms=100)

    @pytest.mark.asyncio
    async def test_filter_predicate_applied(self):
        """Optional filter predicate is applied to broadcast messages."""
        records = [
            CassetteRecord(
                type="broadcast", event="cpu.stepping",
                message={"event": "cpu.stepping", "stepping": True, "pc": 1},
                timestamp=1.0,
            ),
            CassetteRecord(
                type="broadcast", event="cpu.stepping",
                message={"event": "cpu.stepping", "stepping": True, "pc": 2},
                timestamp=2.0,
            ),
        ]
        replay = _build_replay(records)

        msg = await replay.wait_for_broadcast(
            "cpu.stepping",
            timeout_ms=1000,
            filter=lambda m: m.get("pc") == 2,
        )
        assert msg["pc"] == 2


# ---------- Interface compatibility with FakeTransport ----------


class TestFakeTransportInterfaceCompat:
    """ReplayTransport implements the FakeTransport-compatible interface."""

    def test_replay_set_state_replaces_current_state(self):
        """set_state replaces the state dict (seeds initial state)."""
        replay = _build_replay([])
        replay.set_state({"stepping": True, "custom": "value"})
        assert replay.state == {"stepping": True, "custom": "value"}

    def test_replay_state_property_returns_copy(self):
        """state property returns a copy, not the internal dict."""
        replay = _build_replay([])
        state = replay.state
        state["mutated"] = True
        assert "mutated" not in replay.state

    def test_set_response_is_noop_with_warning(self, caplog):
        """set_response is a no-op (responses come from cassette)."""
        import logging

        replay = _build_replay([])
        with caplog.at_level(logging.WARNING):
            replay.set_response("memory.read_u32", {"value": 1})
        assert any("no-op" in r.message for r in caplog.records)

    def test_set_faf_handler_is_noop_with_warning(self, caplog):
        """set_faf_handler is a no-op (side effects come from cassette)."""
        import logging

        replay = _build_replay([])
        with caplog.at_level(logging.WARNING):
            replay.set_faf_handler("cpu.stepping", lambda **kw: None)
        assert any("no-op" in r.message for r in caplog.records)

    def test_push_broadcast_enqueues_message(self):
        """push_broadcast enqueues a message on the events queue."""
        replay = _build_replay([])
        msg = {"event": "test"}
        replay.push_broadcast(msg)
        assert replay.events.qsize() == 1

    def test_events_property_returns_queue(self):
        """events property returns the internal asyncio.Queue."""
        replay = _build_replay([])
        assert isinstance(replay.events, asyncio.Queue)

    def test_calls_attribute_records_invocations(self):
        """calls attribute records call() invocations (FakeTransport compat)."""
        replay = _build_replay([])
        # Note: actual call would raise CassetteExhausted since no records.
        # Just verify the attribute exists and is a list.
        assert hasattr(replay, "calls")
        assert isinstance(replay.calls, list)


# ---------- from_cassette end-to-end ----------


class TestFromCassette:
    """ReplayTransport.from_cassette loads from a JSONL file."""

    @pytest.mark.asyncio
    async def test_from_cassette_roundtrip(self, tmp_path: Path):
        """Record via RecordingTransport → flush → load via from_cassette."""
        from record_replay.recording_transport import RecordingTransport

        # Build a fake real transport with a state-changing side effect.
        class _Fake:
            def __init__(self):
                self._state = {"stepping": False, "pc": 0x08804000}
                self._events_queue = asyncio.Queue()

            @property
            def events(self):
                return self._events_queue

            async def call(self, event, timeout=5.0, **params):
                if event == "cpu.status":
                    return dict(self._state)
                return {"value": 0xDEADBEEF}

            async def fire_and_forget(self, event, **params):
                self._state = {**self._state, "stepping": True}

        real = _Fake()
        cassette_path = tmp_path / "cassette.jsonl"
        recorder = RecordingTransport(real, cassette_path=cassette_path)

        await recorder.call("memory.read_u32", address=0x08804000)
        await recorder.fire_and_forget("cpu.stepping", step="into")
        recorder.flush()

        # Load via from_cassette and replay.
        replay = ReplayTransport.from_cassette(cassette_path)
        replay.set_state({"stepping": False, "pc": 0x08804000})

        result = await replay.call("memory.read_u32", address=0x08804000)
        assert result == {"value": 0xDEADBEEF}

        await replay.fire_and_forget("cpu.stepping", step="into")
        # State should be MERGED: stepping=True, pc preserved.
        assert replay.state["stepping"] is True
        assert replay.state["pc"] == 0x08804000


# ---------- Introspection properties ----------


class TestReplayIntrospection:
    """remaining_calls / remaining_faf / remaining_broadcasts track cursor."""

    @pytest.mark.asyncio
    async def test_remaining_calls_decrements(self):
        records = [
            CassetteRecord(type="call", event="a", response={}, timestamp=1.0),
            CassetteRecord(type="call", event="b", response={}, timestamp=2.0),
        ]
        replay = _build_replay(records)
        assert replay.remaining_calls == 2

        await replay.call("a")
        assert replay.remaining_calls == 1

        await replay.call("b")
        assert replay.remaining_calls == 0

    @pytest.mark.asyncio
    async def test_remaining_faf_decrements(self):
        records = [
            CassetteRecord(type="fire_and_forget", event="x", params={}, timestamp=1.0),
            CassetteRecord(type="fire_and_forget", event="y", params={}, timestamp=2.0),
        ]
        replay = _build_replay(records)
        assert replay.remaining_faf == 2

        await replay.fire_and_forget("x")
        assert replay.remaining_faf == 1
