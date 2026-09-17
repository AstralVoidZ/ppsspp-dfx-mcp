"""Fixture loader — loads contract fixtures into a FakeTransport.

Loads the per-event fixture files produced by ContractRecorder.flush()
and injects them into a FakeTransport via set_response. Two modes:

- load_all (default): each event gets its first record's response
  (simple, sufficient for most L1 contract tests).
- load_with_params: each event gets a callable that matches the call
  params against recorded params, returning the best-matching response
  (for events with param-dependent responses).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _load_fixture_file(path: Path) -> tuple[str, str, list[dict[str, Any]]]:
    """Load one fixture file.

    Returns:
        (event_name, record_type, records) — records is a list of
        {params, response} (call), {params} (fire_and_forget), or
        {message} (broadcast).
    """
    event = path.stem  # filename without .json
    data = json.loads(path.read_text(encoding="utf-8"))
    record_type = data.get("type", "call")
    records = data.get("records", [])
    ppsspp_version = data.get("ppsspp_version", "unknown")
    logger.debug(
        "loaded fixture %s: type=%s, %d records, ppsspp_version=%s",
        event,
        record_type,
        len(records),
        ppsspp_version,
    )
    return event, record_type, records


def load_all(fixture_dir: Path, fake_transport: Any) -> dict[str, int]:
    """Load all fixtures from `fixture_dir` into `fake_transport`.

    For each <event>.json file, calls `fake_transport.set_response(event,
    response)` with the first record's response. For fire_and_forget
    fixtures, no set_response is called (faF side effects are
    transport-internal).

    Returns:
        A dict mapping event name -> number of records loaded.
    """
    if not fixture_dir.is_dir():
        logger.warning("fixture dir not found: %s", fixture_dir)
        return {}

    counts: dict[str, int] = {}
    for path in sorted(fixture_dir.glob("*.json")):
        event, record_type, records = _load_fixture_file(path)
        counts[event] = len(records)
        if record_type == "call" and records:
            first_response = records[0].get("response", {})
            fake_transport.set_response(event, dict(first_response))
        # fire_and_forget and broadcast fixtures are not injected via
        # set_response — they require set_faf_handler / push_broadcast,
        # which is transport-specific behavior not covered by load_all.
    logger.info(
        "load_all: %d events loaded into %s",
        len(counts),
        type(fake_transport).__name__,
    )
    return counts


def load_with_params(fixture_dir: Path, fake_transport: Any) -> dict[str, int]:
    """Load all fixtures with params matching.

    For each <event>.json file, calls `fake_transport.set_response(event,
    matcher)` where matcher is a callable that receives the call params
    and returns the best-matching record's response. If no exact param
    match, falls back to the first record's response.

    Returns:
        A dict mapping event name -> number of records loaded.
    """
    if not fixture_dir.is_dir():
        logger.warning("fixture dir not found: %s", fixture_dir)
        return {}

    counts: dict[str, int] = {}
    for path in sorted(fixture_dir.glob("*.json")):
        event, record_type, records = _load_fixture_file(path)
        counts[event] = len(records)
        if record_type == "call" and records:

            def make_matcher(recs: list[dict[str, Any]]) -> Callable[..., dict[str, Any]]:
                def matcher(**params: Any) -> dict[str, Any]:
                    # Exact param match first.
                    for r in recs:
                        if r.get("params") == params:
                            return dict(r.get("response", {}))
                    # Fallback to first record.
                    return dict(recs[0].get("response", {}))

                return matcher

            fake_transport.set_response(event, make_matcher(records))
    logger.info(
        "load_with_params: %d events loaded into %s",
        len(counts),
        type(fake_transport).__name__,
    )
    return counts


def check_ppsspp_version(fixture_dir: Path, expected_version: str | None = None) -> list[str]:
    """Check fixture files for ppsspp_version metadata.

    If `expected_version` is given, logs a warning for any fixture
    whose recorded version differs (drift detection). Returns the list
    of versions found across all fixture files.
    """
    if not fixture_dir.is_dir():
        return []
    versions: list[str] = []
    for path in sorted(fixture_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        v = data.get("ppsspp_version", "unknown")
        if v not in versions:
            versions.append(v)
        if expected_version and v != expected_version and v != "unknown":
            logger.warning(
                "fixture %s: ppsspp_version mismatch (recorded=%s, expected=%s) — "
                "consider re-recording fixtures if PPSSPP behavior changed",
                path.name,
                v,
                expected_version,
            )
    return versions
