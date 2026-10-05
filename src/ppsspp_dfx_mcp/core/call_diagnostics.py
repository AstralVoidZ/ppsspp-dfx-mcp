"""Ticketed-call diagnostics.

Why this exists
---------------
Measured 2026-09-30 on the real TOPX image: ``gpu.stats.get`` works
on a fresh session and then times out permanently once the game has been
running for a while. The tool blamed "game loading", but screenshots taken
at the same moment showed a healthy 60 fps render -- so the message pointed
at the wrong cause and an agent would wait for a load that had finished.

``WsTransport.call`` is the only place that sees BOTH halves of a ticketed
exchange (the send and the matching response, or its absence), so the
record is taken here rather than in any individual tool.

A correction to an earlier hypothesis, kept deliberately: this was first
blamed on the 60 fps ``gpu.stats.feed`` broadcast crowding the response
channel. That was wrong -- ``start_gpu_stats_feed()`` has zero call sites,
so the feed is never enabled. The real mechanism is that ``gpu.stats.get``
is *ticketed*: PPSSPP answers it on the next GPU flip, so a session whose
CPU is not advancing frames never produces the response. The record below
exists to pin that down with data instead of inference.

Scope note: this module only RECORDS. It deliberately does not classify or
reword errors -- the error contract requires the three-way attribution, and that
classification must be written once the data is in.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

# Bounded so a long session cannot grow without limit; the whole point of
# the audit trail is the recent past, not an unbounded log.
DEFAULT_CAPACITY = 200


@dataclass
class CallRecord:
    """Lifecycle of one ticketed call.

    Field set is fixed by the diagnostics contract; a missing field makes the record
    useless for the question it exists to answer, so the tests assert on
    the exact set rather than on truthiness.
    """

    event: str
    ticket: str
    issued_at: float
    #: None while the call is still in flight, else seconds waited.
    elapsed_s: float | None = None
    #: True once a response (or a timeout) settled the call.
    settled: bool = False
    #: True when the call ended in a timeout.
    timed_out: bool = False
    #: Transport-level error text, when the call failed that way.
    error: str | None = None
    #: Stepping state observed when the call settled, when knowable.
    stepping_at_timeout: bool | None = None
    #: Monotonic timestamp of the most recent broadcast of the same event
    #: seen while this call was in flight. Distinguishes "no response at
    #: all" from "responses are flowing but not matched to this ticket".
    last_same_event_broadcast_at: float | None = None
    #: Free-form extras a caller wants to attach (never required).
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CallDiagnostics:
    """Ring buffer of recent ticketed calls, one instance per transport."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {capacity}")
        self._records: deque[CallRecord] = deque(maxlen=capacity)

    # -- recording -------------------------------------------------------

    def begin(self, event: str, ticket: str) -> CallRecord:
        """Register a call as in flight and return its record."""
        rec = CallRecord(event=event, ticket=ticket, issued_at=time.monotonic())
        self._records.append(rec)
        return rec

    def settle(
        self,
        rec: CallRecord,
        *,
        timed_out: bool = False,
        error: str | None = None,
        stepping: bool | None = None,
    ) -> CallRecord:
        """Mark a call finished and fill in the outcome fields."""
        rec.elapsed_s = round(time.monotonic() - rec.issued_at, 6)
        rec.settled = True
        rec.timed_out = timed_out
        rec.error = error
        rec.stepping_at_timeout = stepping
        return rec

    def note_broadcast(self, event: str) -> None:
        """Record that a broadcast for `event` arrived.

        Stamped onto every still-pending call of the same event: if a timed
        out call had a recent same-event broadcast, the response path is
        alive and the ticket pairing is what failed.
        """
        now = time.monotonic()
        for rec in self._records:
            if not rec.settled and rec.event == event:
                rec.last_same_event_broadcast_at = now

    # -- reading ---------------------------------------------------------

    def recent(self, event: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Snapshot of records, newest last, optionally filtered by event."""
        items = [r for r in self._records if event is None or r.event == event]
        if limit is not None:
            items = items[-limit:]
        return [r.to_dict() for r in items]

    def last(self, event: str | None = None) -> dict[str, Any] | None:
        """Most recent record, or None when nothing matches."""
        items = [r for r in self._records if event is None or r.event == event]
        return items[-1].to_dict() if items else None

    def timeouts(self, event: str | None = None) -> list[dict[str, Any]]:
        """Only the calls that ended in a timeout."""
        return [r for r in self.recent(event) if r["timed_out"]]

    def clear(self) -> None:
        self._records.clear()

    def __len__(self) -> int:
        return len(self._records)
