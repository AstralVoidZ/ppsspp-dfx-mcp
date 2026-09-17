"""Tests for RecordingTransport (recording_transport.py).

Verifies that RecordingTransport:
- Decorates a real transport (composition), forwarding all calls
- Records call / fire_and_forget / broadcast / state_change records
- flush() writes the in-memory buffer to a JSONL cassette file
- State-change capture reads cpu.status before/after fire_and_forget
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from record_replay.cassette import CassetteRecord, load_cassette
from record_replay.recording_transport import RecordingTransport

# ---------- Test doubles ----------


class FakeRealTransport:
    """A fake 'real transport' for testing RecordingTransport.

    Simulates WsTransport's implicit protocol (call / fire_and_forget /
    wait_for_broadcast / events) with configurable responses and a
    mutable state that fire_and_forget can change.
    """

    def __init__(self) -> None:
        self._responses: dict[str, dict[str, Any]] = {}
        self._state: dict[str, Any] = {"stepping": False, "pc": 0x08804000}
        self._faf_side_effects: dict[str, dict[str, Any]] = {}
        self._events_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def set_response(self, event: str, response: dict[str, Any]) -> None:
        self._responses[event] = response

    def set_faf_side_effect(self, event: str, state_delta: dict[str, Any]) -> None:
        """Configure fire_and_forget to mutate the state when called."""
        self._faf_side_effects[event] = state_delta

    @property
    def events(self) -> asyncio.Queue[dict[str, Any]]:
        return self._events_queue

    @property
    def host(self) -> str:
        return "127.0.0.1"

    @property
    def port(self) -> int:
        return 12345

    async def call(self, event: str, timeout: float = 5.0, **params: Any) -> dict[str, Any]:
        if event == "cpu.status":
            return dict(self._state)
        return dict(self._responses.get(event, {}))

    async def fire_and_forget(self, event: str, **params: Any) -> None:
        # Apply the configured side effect (mutate state).
        if event in self._faf_side_effects:
            self._state = {**self._state, **self._faf_side_effects[event]}

    async def wait_for_state(self, predicate, timeout_ms=3000, interval_ms=50):
        return dict(self._state)

    async def wait_for_broadcast(self, event, timeout_ms=5000, filter=None):
        msg = await asyncio.wait_for(self._events_queue.get(), timeout=timeout_ms / 1000.0)
        return msg


# ---------- Tests ----------


class TestRecordingTransportCall:
    """RecordingTransport.call forwards and records."""

    @pytest.mark.asyncio
    async def test_call_forwarded_and_recorded(self):
        real = FakeRealTransport()
        real.set_response("memory.read_u32", {"value": 0xDEADBEEF})
        recorder = RecordingTransport(real)

        result = await recorder.call("memory.read_u32", address=0x08804000)

        assert result == {"value": 0xDEADBEEF}
        records = recorder.records
        assert len(records) == 1
        assert records[0].type == "call"
        assert records[0].event == "memory.read_u32"
        assert records[0].params == {"address": 0x08804000}
        assert records[0].response == {"value": 0xDEADBEEF}

    @pytest.mark.asyncio
    async def test_multiple_calls_recorded_in_order(self):
        real = FakeRealTransport()
        real.set_response("memory.read_u32", {"value": 1})
        real.set_response("cpu.status", {"stepping": False})
        recorder = RecordingTransport(real, capture_state_changes=False)

        await recorder.call("memory.read_u32", address=0x08804000)
        await recorder.call("cpu.status")

        records = recorder.records
        assert len(records) == 2
        assert records[0].event == "memory.read_u32"
        assert records[1].event == "cpu.status"


class TestRecordingTransportFireAndForget:
    """RecordingTransport.fire_and_forget records and captures state changes."""

    @pytest.mark.asyncio
    async def test_fire_and_forget_recorded_without_state_capture(self):
        real = FakeRealTransport()
        recorder = RecordingTransport(real, capture_state_changes=False)

        await recorder.fire_and_forget("cpu.stepping", step="into")

        records = recorder.records
        assert len(records) == 1
        assert records[0].type == "fire_and_forget"
        assert records[0].event == "cpu.stepping"
        assert records[0].params == {"step": "into"}

    @pytest.mark.asyncio
    async def test_state_change_captured_when_state_mutates(self):
        """When fire_and_forget mutates state, a state_change record is added."""
        real = FakeRealTransport()
        # Configure fire_and_forget("cpu.stepping") to flip stepping=True
        real.set_faf_side_effect("cpu.stepping", {"stepping": True})
        recorder = RecordingTransport(real, capture_state_changes=True)

        await recorder.fire_and_forget("cpu.stepping", step="into")

        records = recorder.records
        # 1 faf record + 1 state_change record
        assert len(records) == 2
        assert records[0].type == "fire_and_forget"
        assert records[1].type == "state_change"
        assert records[1].state_delta == {"stepping": True}

    @pytest.mark.asyncio
    async def test_no_state_change_record_when_state_unchanged(self):
        """If fire_and_forget doesn't change state, no state_change record."""
        real = FakeRealTransport()
        # No side effect configured — state won't change
        recorder = RecordingTransport(real, capture_state_changes=True)

        await recorder.fire_and_forget("cpu.stepping", step="into")

        records = recorder.records
        # Only the faf record, no state_change
        assert len(records) == 1
        assert records[0].type == "fire_and_forget"


class TestRecordingTransportBroadcast:
    """RecordingTransport.wait_for_broadcast records the received message."""

    @pytest.mark.asyncio
    async def test_broadcast_recorded(self):
        real = FakeRealTransport()
        broadcast_msg = {"event": "cpu.stepping", "stepping": True}
        real._events_queue.put_nowait(broadcast_msg)
        recorder = RecordingTransport(real, capture_state_changes=False)

        msg = await recorder.wait_for_broadcast("cpu.stepping", timeout_ms=1000)

        assert msg == broadcast_msg
        records = recorder.records
        assert len(records) == 1
        assert records[0].type == "broadcast"
        assert records[0].event == "cpu.stepping"
        assert records[0].message == broadcast_msg


class TestRecordingTransportFlush:
    """flush() writes the in-memory buffer to a JSONL cassette file."""

    @pytest.mark.asyncio
    async def test_flush_writes_cassette_file(self, tmp_path: Path):
        real = FakeRealTransport()
        real.set_response("memory.read_u32", {"value": 0xDEADBEEF})
        cassette_path = tmp_path / "cassette.jsonl"
        recorder = RecordingTransport(
            real, cassette_path=cassette_path, capture_state_changes=False
        )

        await recorder.call("memory.read_u32", address=0x08804000)
        recorder.flush()

        assert cassette_path.exists()
        # Cassette must be valid JSONL
        loaded = load_cassette(cassette_path)
        assert len(loaded) == 1
        assert loaded[0].type == "call"
        assert loaded[0].event == "memory.read_u32"

    @pytest.mark.asyncio
    async def test_flush_noop_when_cassette_path_none(self, tmp_path: Path):
        """When cassette_path is None, flush is a no-op (records stay in-memory)."""
        real = FakeRealTransport()
        real.set_response("cpu.status", {"stepping": False})
        recorder = RecordingTransport(real, cassette_path=None)

        await recorder.call("cpu.status")
        recorder.flush()  # should not raise

        # Records are still accessible in-memory
        assert len(recorder.records) == 1


class TestRecordingTransportPassthrough:
    """Properties and passthrough methods work correctly."""

    @pytest.mark.asyncio
    async def test_events_property_passthrough(self):
        real = FakeRealTransport()
        recorder = RecordingTransport(real)
        assert recorder.events is real.events

    def test_host_port_passthrough(self):
        real = FakeRealTransport()
        recorder = RecordingTransport(real)
        assert recorder.host == "127.0.0.1"
        assert recorder.port == 12345

    def test_records_property_returns_copy(self):
        """records property returns a copy, not the internal list."""
        real = FakeRealTransport()
        recorder = RecordingTransport(real, capture_state_changes=False)
        # Mutate the returned list — internal must not change.
        records_copy = recorder.records
        records_copy.append("fake")
        assert len(recorder.records) == 0

    def test_clear_empties_buffer(self):
        real = FakeRealTransport()
        recorder = RecordingTransport(real, capture_state_changes=False)
        recorder._records.append(
            CassetteRecord(type="call", event="test", response={}, timestamp=1.0)
        )
        recorder.clear()
        assert len(recorder.records) == 0
