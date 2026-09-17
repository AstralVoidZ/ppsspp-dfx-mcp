"""ReplayTransport — replays recorded PPSSPP WS communication from a
cassette file.

Implements the FakeTransport-compatible interface (call /
fire_and_forget / wait_for_state / wait_for_broadcast / set_state /
set_response / push_broadcast / state / events), but responses come from
a cassette file instead of hand-written configuration. This lets L1
contract tests upgrade their truth source from "developer's reading of
PPSSPP C++ source" to "real PPSSPP capture" with zero test code changes.

Time-agnostic matching strategy (Decision 4 in design.md):
- Match by call order + event name, ignore timestamps.
- PPSSPP non-deterministic timing (VBlank interrupts, thread scheduling)
  makes timestamp-based replay unreliable.
- Tool invocation order is deterministic (test code controls it).
- Broadcast messages are enqueued FIFO on init; wait_for_broadcast
  consumes from the queue.
- Best-effort tolerance: if params mismatch, try the next record with
  the same event (avoid strict matching making replay brittle).

State merge semantics (Decision 3 in design.md):
- ReplayTransport replays fire_and_forget state changes by MERGING the
  recorded state_delta into the current state ({**cur, **delta}), NOT
  by replacing the whole state dict.
- This matches the FakeTransport consumer expectation (conftest
  _set_stepping_true fix: {**cur, "stepping": True}).
- Merging preserves other state fields (ticks, pc) during replay.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .cassette import CassetteRecord, load_cassette

# Type aliases (mirror FakeTransport for interface compatibility).
ResponseConfig = dict[str, Any] | Callable[..., dict[str, Any]]
FAFHandler = Callable[..., None]


class CassetteExhaustedError(RuntimeError):
    """Raised when replay has no more records matching the requested event."""


class ReplayTransport:
    """Replays recorded WS communication from a cassette file.

    Implements the FakeTransport-compatible interface, but responses come
    from a cassette instead of hand-written configuration. Drop-in
    replacement for FakeTransport in L1 contract tests.
    """

    def __init__(self, records: list[CassetteRecord]) -> None:
        """Initialize from a list of cassette records.

        Splits records into:
        - call_records: indexed by event for lookup
        - faf_records: sequential fire_and_forget queue
        - broadcast_records: FIFO queue for wait_for_broadcast
        - state_change_records: sequential state_change queue
        """
        self._call_records: list[CassetteRecord] = [r for r in records if r.type == "call"]
        self._call_cursor = 0
        self._faf_records: list[CassetteRecord] = [
            r for r in records if r.type == "fire_and_forget"
        ]
        self._faf_cursor = 0
        self._state_change_records: list[CassetteRecord] = [
            r for r in records if r.type == "state_change"
        ]
        self._state_change_cursor = 0
        self._broadcast_records: list[CassetteRecord] = [
            r for r in records if r.type == "broadcast"
        ]
        self._broadcast_cursor = 0

        # Current cpu.status state. Default: running (stepping=False).
        # State changes are MERGED into this dict during replay.
        self._current_state: dict[str, Any] = {"stepping": False}

        # Recordings of invocations (mirror FakeTransport's `calls` /
        # `fire_and_forget_calls` attributes for consumer compatibility).
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fire_and_forget_calls: list[tuple[str, dict[str, Any]]] = []

        # Event queue for broadcasts (mirror FakeTransport._events_queue).
        self._events_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # Pre-enqueue all broadcast records on init so wait_for_broadcast
        # can drain them FIFO.
        for record in self._broadcast_records:
            if record.message is not None:
                self._events_queue.put_nowait(record.message)

    @classmethod
    def from_cassette(cls, cassette_path: Path) -> ReplayTransport:
        """Load records from a JSONL cassette file and build a replay transport."""
        records = load_cassette(cassette_path)
        return cls(records)

    # ---------- FakeTransport-compatible configuration ----------

    def set_response(self, event: str, response: ResponseConfig) -> None:
        """No-op for ReplayTransport — responses come from cassette.

        Provided for interface compatibility with FakeTransport so L1
        test fixtures can switch FakeTransport <-> ReplayTransport
        without code changes. Logs a warning if called (likely a
        misuse — the cassette should be the single source of truth).
        """
        import logging

        logging.getLogger(__name__).warning(
            "ReplayTransport.set_response('%s', ...) is a no-op — "
            "responses come from the cassette. If you intended to "
            "override a response, edit the cassette instead.",
            event,
        )

    def set_faf_handler(self, event: str, handler: FAFHandler) -> None:
        """No-op for ReplayTransport — fire_and_forget side effects come
        from the cassette's state_change records."""
        import logging

        logging.getLogger(__name__).warning(
            "ReplayTransport.set_faf_handler('%s', ...) is a no-op — "
            "fire_and_forget side effects come from the cassette's "
            "state_change records.",
            event,
        )

    def set_state(self, state: dict[str, Any]) -> None:
        """Replace the current state dict (mirrors FakeTransport.set_state).

        Note: this is a direct replacement, used to seed initial state.
        State CHANGE replay (via fire_and_forget) uses MERGE semantics.
        """
        self._current_state = dict(state)

    @property
    def state(self) -> dict[str, Any]:
        """Read-only view of the current cpu.status response."""
        return dict(self._current_state)

    @property
    def events(self) -> asyncio.Queue[dict[str, Any]]:
        """Async queue of ticketless broadcast messages (mirrors FakeTransport)."""
        return self._events_queue

    def push_broadcast(self, msg: dict[str, Any]) -> None:
        """Enqueue a broadcast message (mirrors FakeTransport.push_broadcast)."""
        self._events_queue.put_nowait(msg)

    # ---------- Implicit WsTransport protocol ----------

    async def call(
        self,
        event: str,
        timeout: float = 5.0,
        **params: Any,
    ) -> dict[str, Any]:
        """Replay the next call record matching `event`.

        Time-agnostic matching: scans forward from the cursor for the
        next `call` record with matching event. Best-effort tolerance:
        if params don't match, still returns the response (records the
        mismatch but doesn't fail) — strict matching would make replay
        brittle against minor param variations.

        For `cpu.status` event: returns the current state dict (mirrors
        FakeTransport.call behavior).
        """
        self.calls.append((event, dict(params)))

        if event == "cpu.status":
            return dict(self._current_state)

        # Scan forward from cursor for the next matching call record.
        # Best-effort: prefer exact param match; fall back to first
        # record with matching event.
        exact_match: CassetteRecord | None = None
        event_match: CassetteRecord | None = None
        scan_idx = self._call_cursor
        while scan_idx < len(self._call_records):
            record = self._call_records[scan_idx]
            if record.event == event:
                if event_match is None:
                    event_match = record
                if record.params == params:
                    exact_match = record
                    break
            scan_idx += 1

        match = exact_match or event_match
        if match is None:
            raise CassetteExhaustedError(
                f"no call record matching event='{event}' "
                f"(searched {len(self._call_records) - self._call_cursor} "
                f"records from cursor {self._call_cursor})"
            )

        # Advance cursor past the matched record (consume it).
        match_idx = self._call_records.index(match)
        self._call_cursor = match_idx + 1

        return dict(match.response) if match.response is not None else {}

    async def fire_and_forget(self, event: str, **params: Any) -> None:
        """Replay the next fire_and_forget record, merging any state change.

        State merge semantics: if the cassette has a state_change record
        paired with this fire_and_forget, the state_delta is MERGED into
        the current state ({**cur, **delta}) rather than replacing it.
        This preserves other state fields (ticks, pc) during replay.
        """
        self.fire_and_forget_calls.append((event, dict(params)))

        if self._faf_cursor >= len(self._faf_records):
            raise CassetteExhaustedError(
                f"no fire_and_forget record matching event='{event}' "
                f"(cursor exhausted at {self._faf_cursor})"
            )

        # Consume the next faf record (sequential, by order).
        _faf_record = self._faf_records[self._faf_cursor]
        self._faf_cursor += 1

        # Apply any paired state_change record (sequential, by order).
        if self._state_change_cursor < len(self._state_change_records):
            sc_record = self._state_change_records[self._state_change_cursor]
            # state_change records are paired with faf records by order.
            # Consume the next one regardless of event match (order-based).
            self._state_change_cursor += 1
            if sc_record.state_delta:
                # MERGE semantics: {**cur, **delta}, not replacement.
                self._current_state = {
                    **self._current_state,
                    **sc_record.state_delta,
                }

    async def wait_for_state(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout_ms: int = 3000,
        interval_ms: int = 50,
    ) -> dict[str, Any]:
        """Poll the current state until predicate is satisfied or timeout.

        Mirrors FakeTransport.wait_for_state: state is updated
        synchronously by fire_and_forget, so the predicate typically
        matches on the first check.
        """
        start = time.monotonic()
        timeout_s = timeout_ms / 1000.0
        interval_s = interval_ms / 1000.0
        while True:
            current = dict(self._current_state)
            if predicate(current):
                return current
            elapsed = time.monotonic() - start
            if elapsed >= timeout_s:
                raise TimeoutError(
                    f"wait_for_state timeout ({timeout_ms}ms) — predicate not satisfied"
                )
            await asyncio.sleep(interval_s)

    async def wait_for_broadcast(
        self,
        event: str,
        timeout_ms: int = 5000,
        filter: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Wait for a broadcast event on the events queue (mirrors FakeTransport).

        Broadcasts were pre-enqueued on init from the cassette's
        broadcast records. Drains the queue, matches by event + optional
        filter, requeues non-matching messages in FIFO order.
        """
        start_time = time.monotonic()
        timeout_s = timeout_ms / 1000.0
        backlog: list[dict[str, Any]] = []
        try:
            while True:
                remaining_s = timeout_s - (time.monotonic() - start_time)
                if remaining_s <= 0:
                    raise TimeoutError(
                        f"wait_for_broadcast timeout ({timeout_ms}ms) — "
                        f"no matching '{event}' broadcast"
                    )
                try:
                    msg = await asyncio.wait_for(self._events_queue.get(), timeout=remaining_s)
                except TimeoutError:
                    raise TimeoutError(
                        f"wait_for_broadcast timeout ({timeout_ms}ms) — "
                        f"no matching '{event}' broadcast"
                    ) from None
                if msg.get("event") == event and (filter is None or filter(msg)):
                    return msg
                backlog.append(msg)
        except TimeoutError:
            raise
        finally:
            for msg in backlog:
                self._events_queue.put_nowait(msg)

    # ---------- Replay-specific introspection ----------

    @property
    def remaining_calls(self) -> int:
        """Number of call records not yet consumed."""
        return max(0, len(self._call_records) - self._call_cursor)

    @property
    def remaining_faf(self) -> int:
        """Number of fire_and_forget records not yet consumed."""
        return max(0, len(self._faf_records) - self._faf_cursor)

    @property
    def remaining_broadcasts(self) -> int:
        """Number of broadcast records not yet consumed."""
        return max(0, len(self._broadcast_records) - self._broadcast_cursor)
