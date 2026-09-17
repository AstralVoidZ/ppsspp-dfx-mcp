"""Record-Replay transport layer for ppsspp-dfx-mcp integration tests.

Records real PPSSPP WebSocket communication to a cassette file (JSONL),
then replays it for L1 contract regression tests. Uses the time-agnostic
matching strategy (by call order + event name, ignoring timestamps) to
tolerate PPSSPP's non-deterministic timing (VBlank interrupts, thread
scheduling).

Components:
- RecordingTransport — decorator over WsTransport, records all
  communication (call / fire_and_forget / broadcast / state_change)
  to an in-memory buffer, flushed to a JSONL cassette file.
- ReplayTransport — implements the FakeTransport-compatible interface,
  but responses come from a cassette file instead of hand-written
  configuration. State changes are MERGED into the current state
  (not replaced), matching the FakeTransport consumer expectation
  (see conftest _set_stepping_true fix: {**cur, "stepping": True}).
- Cassette format — JSONL (one JSON record per line), each record has
  type / event / params / response / message / timestamp fields.
"""

from __future__ import annotations

from .cassette import (
    CassetteRecord,
    RecordType,
    load_cassette,
    save_cassette,
)
from .recording_transport import RecordingTransport
from .replay_transport import ReplayTransport

__all__ = [
    "RecordingTransport",
    "ReplayTransport",
    "CassetteRecord",
    "RecordType",
    "load_cassette",
    "save_cassette",
]
