"""Contract fixture recorder — captures per-event PPSSPP responses as
composable JSON fixtures (one file per event).

Complementary to the cassette (record_replay.cassette):
- Cassette is a SEQUENTIAL stream of all communication (for regression
  replay of full sessions).
- Fixtures are PER-EVENT slices (for L1 contract tests that exercise
  a single event in isolation, composable across tests).

Design decision (design.md Decision 5): ContractRecorder groups
recorded responses by event name, so L1 tests can pick any event's
fixture file without scanning the whole cassette.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from record_replay.cassette import load_cassette

logger = logging.getLogger(__name__)


class ContractRecorder:
    """Records PPSSPP responses grouped by event name.

    Decorates a real WsTransport (or RecordingTransport) and builds an
    in-memory dict: event_name -> list of {params, response} records.
    `flush()` writes one JSON file per event to `fixtures/<event>.json`.
    """

    def __init__(
        self,
        real_transport: Any,
        ppsspp_version: str | None = None,
    ) -> None:
        """Initialize the contract recorder.

        Args:
            real_transport: the WsTransport (or RecordingTransport) to
                wrap. Must implement call / fire_and_forget.
            ppsspp_version: optional version string recorded in each
                fixture file's metadata, for drift detection.
        """
        self._real = real_transport
        self._ppsspp_version = ppsspp_version
        # event -> list of {params, response} for call records.
        self._call_fixtures: dict[str, list[dict[str, Any]]] = {}
        # event -> list of {params} for fire_and_forget records (no response).
        self._faf_fixtures: dict[str, list[dict[str, Any]]] = {}
        # event -> list of {message} for broadcast records.
        self._broadcast_fixtures: dict[str, list[dict[str, Any]]] = {}

    @property
    def call_fixtures(self) -> dict[str, list[dict[str, Any]]]:
        """Read-only access to the in-memory call fixtures dict."""
        return {k: list(v) for k, v in self._call_fixtures.items()}

    async def call(self, event: str, timeout: float = 5.0, **params: Any) -> dict[str, Any]:
        """Forward call to real transport and record the response by event."""
        response = await self._real.call(event, timeout=timeout, **params)
        self._call_fixtures.setdefault(event, []).append(
            {"params": dict(params), "response": dict(response)}
        )
        return response

    async def fire_and_forget(self, event: str, **params: Any) -> None:
        """Forward fire_and_forget to real transport and record by event."""
        await self._real.fire_and_forget(event, **params)
        self._faf_fixtures.setdefault(event, []).append({"params": dict(params)})

    async def wait_for_broadcast(
        self,
        event: str,
        timeout_ms: int = 5000,
        filter: Any | None = None,
    ) -> dict[str, Any]:
        """Forward to real transport's wait_for_broadcast and record the message."""
        msg = await self._real.wait_for_broadcast(event, timeout_ms=timeout_ms, filter=filter)
        self._broadcast_fixtures.setdefault(event, []).append({"message": dict(msg)})
        return msg

    def flush(self, fixture_dir: Path) -> dict[str, int]:
        """Write all fixtures to `fixture_dir/<event>.json`.

        Each fixture file is a JSON array of records. The file includes
        an optional `ppsspp_version` metadata field at the top level.

        Returns:
            A dict mapping event name -> number of records written.
        """
        fixture_dir.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}

        # Write call fixtures (one file per event).
        for event, records in self._call_fixtures.items():
            path = fixture_dir / f"{event}.json"
            payload = {
                "ppsspp_version": self._ppsspp_version,
                "type": "call",
                "records": records,
            }
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            counts[event] = len(records)

        # Write fire_and_forget fixtures (one file per event).
        for event, records in self._faf_fixtures.items():
            path = fixture_dir / f"{event}.json"
            payload = {
                "ppsspp_version": self._ppsspp_version,
                "type": "fire_and_forget",
                "records": records,
            }
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            counts[event] = len(records)

        # Write broadcast fixtures (one file per event).
        for event, records in self._broadcast_fixtures.items():
            path = fixture_dir / f"{event}.json"
            payload = {
                "ppsspp_version": self._ppsspp_version,
                "type": "broadcast",
                "records": records,
            }
            path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            counts[event] = len(records)

        logger.info(
            "contract fixtures flushed: %d events, %d total records",
            len(counts),
            sum(counts.values()),
        )
        return counts

    @classmethod
    def from_cassette(
        cls,
        cassette_path: Path,
        ppsspp_version: str | None = None,
    ) -> ContractRecorder:
        """Build a ContractRecorder from a recorded cassette file.

        Useful for deriving fixtures from an existing cassette without
        re-recording against a live PPSSPP. The returned recorder has
        its in-memory fixtures populated from the cassette; `flush()`
        writes them to disk.

        If `ppsspp_version` is None, auto-extracts the version from the
        cassette's `version` event record (response.version field).
        This eliminates the need for a separate version detection
        mechanism in the recording script — the version event is
        already part of the recorded cassette.
        """
        records = load_cassette(cassette_path)
        if ppsspp_version is None:
            for r in records:
                if r.type == "call" and r.event == "version":
                    response = r.response or {}
                    v = response.get("version")
                    if v:
                        ppsspp_version = v
                    break
        recorder = cls(real_transport=None, ppsspp_version=ppsspp_version)
        for record in records:
            if record.type == "call":
                recorder._call_fixtures.setdefault(record.event, []).append(
                    {
                        "params": dict(record.params or {}),
                        "response": dict(record.response or {}),
                    }
                )
            elif record.type == "fire_and_forget":
                recorder._faf_fixtures.setdefault(record.event, []).append(
                    {"params": dict(record.params or {})}
                )
            elif record.type == "broadcast":
                recorder._broadcast_fixtures.setdefault(record.event, []).append(
                    {"message": dict(record.message or {})}
                )
        return recorder
