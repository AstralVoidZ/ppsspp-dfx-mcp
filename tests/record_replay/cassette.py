"""Cassette format — JSONL storage for recorded PPSSPP WS communication.

Each line is a self-contained JSON record. The schema:

    {
      "type": "call" | "fire_and_forget" | "broadcast" | "state_change",
      "event": "<ws_event_name>",
      "timestamp": <float_epoch_seconds>,
      # for type == "call":
      "params": {<call_params>},
      "response": {<ppsspp_response>},
      # for type == "broadcast":
      "message": {<broadcast_payload>},
      # for type == "state_change":
      "state_delta": {<partial_state_to_merge>}
    }

JSONL (one JSON per line) is chosen over a single JSON array for:
- streaming writes: flush incrementally without buffering all records
- line-level parsing: low memory, can skip corrupted lines
- diff friendliness: each line is a self-contained record
- fault tolerance: a single corrupted line does not invalidate others
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal, Optional

RecordType = Literal["call", "fire_and_forget", "broadcast", "state_change"]


@dataclass
class CassetteRecord:
    """A single recorded communication record.

    Fields are conditionally populated based on `type`:
    - call: event, params, response, timestamp
    - fire_and_forget: event, params, timestamp
    - broadcast: event, message, timestamp
    - state_change: event (the faf event that triggered it), state_delta, timestamp
    """

    type: RecordType
    event: str
    timestamp: float = field(default_factory=time.time)
    params: Optional[dict[str, Any]] = None
    response: Optional[dict[str, Any]] = None
    message: Optional[dict[str, Any]] = None
    state_delta: Optional[dict[str, Any]] = None

    def to_json(self) -> str:
        """Serialize to a single-line JSON string (one cassette line)."""
        payload: dict[str, Any] = {
            "type": self.type,
            "event": self.event,
            "timestamp": self.timestamp,
        }
        if self.params is not None:
            payload["params"] = self.params
        if self.response is not None:
            payload["response"] = self.response
        if self.message is not None:
            payload["message"] = self.message
        if self.state_delta is not None:
            payload["state_delta"] = self.state_delta
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> "CassetteRecord":
        """Deserialize from a single JSON line.

        Raises:
            json.JSONDecodeError: line is not valid JSON.
            KeyError: required fields (type, event) are missing.
        """
        data = json.loads(line)
        return cls(
            type=data["type"],
            event=data["event"],
            timestamp=data.get("timestamp", 0.0),
            params=data.get("params"),
            response=data.get("response"),
            message=data.get("message"),
            state_delta=data.get("state_delta"),
        )


def save_cassette(records: list[CassetteRecord], cassette_path: Path) -> None:
    """Write records to a JSONL cassette file (one JSON per line)."""
    cassette_path.parent.mkdir(parents=True, exist_ok=True)
    with cassette_path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(record.to_json() + "\n")


def load_cassette(cassette_path: Path) -> list[CassetteRecord]:
    """Load records from a JSONL cassette file.

    Tolerates partial corruption: individual lines that fail to parse
    are skipped with a warning logged, rather than aborting the load.
    """
    records: list[CassetteRecord] = []
    with cassette_path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(CassetteRecord.from_json(line))
            except (json.JSONDecodeError, KeyError) as exc:
                # Partial corruption tolerance: skip the bad line.
                import logging

                logging.getLogger(__name__).warning(
                    "cassette %s:%d: skipping malformed line (%s)",
                    cassette_path,
                    lineno,
                    exc,
                )
                continue
    return records


def iter_cassette(cassette_path: Path) -> Iterator[CassetteRecord]:
    """Stream records from a JSONL cassette file (line-by-line iterator)."""
    with cassette_path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield CassetteRecord.from_json(line)
            except (json.JSONDecodeError, KeyError) as exc:
                import logging

                logging.getLogger(__name__).warning(
                    "cassette %s:%d: skipping malformed line (%s)",
                    cassette_path,
                    lineno,
                    exc,
                )
                continue
