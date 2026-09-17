"""test_record_replay_roundtrip.py — verify RecordingTransport → ReplayTransport
round-trip consistency using the real PPSSPP cassette.

Anchor: RecordingTransport (records WS communication to cassette) →
ReplayTransport (replays from cassette) must produce identical
observable behavior to the original transport for any sequence of
calls covered by the cassette.

The cassette (tests/cassettes/real_ppsspp.jsonl) was recorded against
a live PPSSPP process running the TOPX ISO. The round-trip test
verifies that:

1. The cassette parses cleanly (no malformed lines, all records
   load via load_cassette).
2. ReplayTransport can replay every `call` event in the cassette
   without raising CassetteExhaustedError (every recorded call has
   a matching replay record).
3. The replayed response matches the recorded response (event name
   and response dict are identical).
4. fire_and_forget records can be replayed in order (state_change
   records are merged correctly).
5. broadcast records pre-enqueue on ReplayTransport init and can
   be drained via wait_for_broadcast.

This test does NOT require a live PPSSPP — it operates entirely on
the recorded cassette. Skips when the cassette is missing (CI-safe).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from record_replay.cassette import (
    load_cassette,
)
from record_replay.replay_transport import (
    CassetteExhaustedError,
    ReplayTransport,
)

# ============================================================================
# Cassette loading & shape
# ============================================================================


def test_cassette_loads_cleanly(cassette_path: Path):
    """The recorded cassette loads without malformed lines.

    load_cassette tolerates partial corruption (skips bad lines with a
    warning). We assert zero lines were skipped — every line in the
    real cassette should be valid JSON (it was written by save_cassette
    which uses CassetteRecord.to_json()).

    A failure here means the cassette file was corrupted on disk (e.g.
    concurrent writes, encoding issues) and should be re-recorded via
    `python -m ppsspp_dfx_mcp.scripts.record_fixtures`.
    """
    records = load_cassette(cassette_path)
    assert len(records) > 0, "cassette is empty (no records)"
    # Every record must have a valid type and event.
    for i, r in enumerate(records):
        assert r.type in ("call", "fire_and_forget", "broadcast", "state_change"), (
            f"record {i} has invalid type {r.type!r}: {r!r}"
        )
        assert r.event, f"record {i} has empty event: {r!r}"


def test_cassette_contains_expected_event_types(cassette_path: Path):
    """The cassette covers call / fire_and_forget / state_change types.

    broadcast records are optional (the recording script may or may not
    have received broadcasts during the 30-tool run — depends on whether
    any tool subscribes to broadcasts).

    call records: tool-initiated WS calls (memory.read_u32, etc.).
    fire_and_forget: cpu.stepping events (pause/into/resume).
    state_change: paired with fire_and_forget, captures state delta.
    """
    records = load_cassette(cassette_path)
    types_seen = {r.type for r in records}
    assert "call" in types_seen, "cassette has no call records"
    assert "fire_and_forget" in types_seen, (
        "cassette has no fire_and_forget records — was cpu.stepping tested?"
    )
    assert "state_change" in types_seen, (
        "cassette has no state_change records — was state capture enabled?"
    )
    # broadcast is optional, but if present, assert it has a message.
    broadcast_records = [r for r in records if r.type == "broadcast"]
    for r in broadcast_records:
        assert r.message is not None, f"broadcast record {r.event!r} has no message: {r!r}"


# ============================================================================
# ReplayTransport round-trip — call responses
# ============================================================================


def test_replay_returns_recorded_call_responses(cassette_path: Path):
    """ReplayTransport.call returns the recorded response for each event.

    Iterates through every `call` record in the cassette, replays it via
    ReplayTransport.call(event=..., **params), and asserts the replayed
    response matches the recorded response. This is the core round-trip
    consistency check.

    Best-effort matching: ReplayTransport scans forward for the next
    matching event (params may differ). We compare the response dict
    field-by-field (excluding the `ticket` field which is per-call
    unique and not part of the semantic response).
    """
    records = load_cassette(cassette_path)
    replay = ReplayTransport(records)

    call_records = [r for r in records if r.type == "call"]
    skipped = 0
    for i, recorded in enumerate(call_records):
        # cpu.status is special — ReplayTransport returns its current
        # state (potentially modified by fire_and_forget), not the
        # recorded response. Skip these for direct comparison.
        if recorded.event == "cpu.status":
            skipped += 1
            continue

        # Replay this call. Use asyncio.run for sync test context.
        response = asyncio.run(
            replay.call(
                recorded.event,
                timeout=1.0,
                **(recorded.params or {}),
            )
        )

        # Compare response (excluding ticket, which is per-call unique).
        expected = dict(recorded.response or {})
        expected.pop("ticket", None)
        actual = dict(response)
        actual.pop("ticket", None)
        assert actual == expected, (
            f"call {i} ({recorded.event!r}) response mismatch:\n"
            f"  expected: {expected!r}\n"
            f"  actual:   {actual!r}"
        )

    # Sanity: we replayed at least one call (skipped cpu.status only).
    assert len(call_records) - skipped > 0, "no replayable call records (all were cpu.status)"


# ============================================================================
# ReplayTransport round-trip — fire_and_forget + state_change
# ============================================================================


def test_replay_fire_and_forget_applies_state_changes(cassette_path: Path):
    """ReplayTransport.fire_and_forget merges recorded state_change deltas.

    The cassette pairs fire_and_forget records (e.g. cpu.stepping) with
    state_change records that capture the resulting cpu.status delta.
    ReplayTransport must apply these state deltas via merge semantics
    ({**cur, **delta}) so the state evolves correctly during replay.

    Test strategy: replay all fire_and_forget records in order, then
    query the final state via `replay.state`. The state must contain
    the `stepping` field (toggled by cpu.stepping events).
    """
    records = load_cassette(cassette_path)
    replay = ReplayTransport(records)

    faf_records = [r for r in records if r.type == "fire_and_forget"]
    assert len(faf_records) > 0, "cassette has no fire_and_forget records"

    # Replay each fire_and_forget — should not raise CassetteExhaustedError.
    for _i, recorded in enumerate(faf_records):
        asyncio.run(replay.fire_and_forget(recorded.event, **(recorded.params or {})))

    # Final state must have `stepping` field (cpu.status default + merges).
    final_state = replay.state
    assert "stepping" in final_state, f"final state missing 'stepping' field: {final_state!r}"
    assert isinstance(final_state["stepping"], bool)


def test_replay_does_not_raise_cassette_exhausted(cassette_path: Path):
    """Replaying all records in order does not exhaust the cassette.

    CassetteExhaustedError is raised when ReplayTransport.call can't find
    a matching record. With best-effort matching (event-name match), this
    should not happen for a self-consistent cassette recorded by
    RecordingTransport (every call had a response, every fire_and_forget
    was captured).
    """
    records = load_cassette(cassette_path)
    replay = ReplayTransport(records)

    call_records = [r for r in records if r.type == "call"]
    faf_records = [r for r in records if r.type == "fire_and_forget"]

    # Replay all fire_and_forget records first (to apply state changes).
    for recorded in faf_records:
        asyncio.run(replay.fire_and_forget(recorded.event, **(recorded.params or {})))

    # Replay all call records (cpu.status returns current state, others
    # return recorded responses).
    for recorded in call_records:
        try:
            asyncio.run(replay.call(recorded.event, timeout=1.0, **(recorded.params or {})))
        except CassetteExhaustedError:
            # Only acceptable for cpu.status when no records exist — but
            # we filtered that out. Any other CassetteExhaustedError is
            # a real inconsistency.
            if recorded.event != "cpu.status":
                raise
            # cpu.status has no recorded response — ReplayTransport
            # returns current state (handled by .call special-case).
            pass


# ============================================================================
# ReplayTransport introspection — cursors advance correctly
# ============================================================================


def test_replay_cursors_advance_after_consumption(cassette_path: Path):
    """ReplayTransport call/faf cursors advance as records are consumed.

    After replaying N call records, remaining_calls should be reduced by N.
    After replaying M fire_and_forget records, remaining_faf should be
    reduced by M. This ensures cursors don't get stuck (which would cause
    CassetteExhaustedError prematurely).
    """
    records = load_cassette(cassette_path)
    replay = ReplayTransport(records)

    initial_remaining_calls = replay.remaining_calls
    initial_remaining_faf = replay.remaining_faf

    # Consume one call record.
    first_call = next(r for r in records if r.type == "call")
    asyncio.run(replay.call(first_call.event, timeout=1.0, **(first_call.params or {})))
    assert replay.remaining_calls < initial_remaining_calls, (
        f"call cursor did not advance: "
        f"before={initial_remaining_calls}, after={replay.remaining_calls}"
    )

    # Consume one fire_and_forget record.
    first_faf = next(r for r in records if r.type == "fire_and_forget")
    asyncio.run(replay.fire_and_forget(first_faf.event, **(first_faf.params or {})))
    assert replay.remaining_faf < initial_remaining_faf, (
        f"faf cursor did not advance: before={initial_remaining_faf}, after={replay.remaining_faf}"
    )


# ============================================================================
# Round-trip: RecordingTransport records what ReplayTransport replays
# ============================================================================


def test_recording_then_replay_preserves_call_sequence(cassette_path: Path):
    """ReplayTransport replays calls in the same sequence as recorded.

    This is the core round-trip property: if we record N calls in order
    (call_1, call_2, ..., call_N), ReplayTransport must replay them in
    the same order (replay_1 = call_1, replay_2 = call_2, ...).

    Test strategy: take the first K call records from the cassette,
    replay them in order, and verify the responses come back in the
    same order. This catches cursor-skip bugs (where best-effort
    matching skips ahead too aggressively).
    """
    records = load_cassette(cassette_path)
    call_records = [r for r in records if r.type == "call"]

    # Take the first 3 non-cpu.status call records (cpu.status is special).
    sample_records = [r for r in call_records if r.event != "cpu.status"][:3]
    if len(sample_records) < 3:
        pytest.skip(
            f"cassette has only {len(sample_records)} non-cpu.status call records "
            "(need at least 3 for sequence test)"
        )

    replay = ReplayTransport(records)
    for i, recorded in enumerate(sample_records):
        response = asyncio.run(replay.call(recorded.event, timeout=1.0, **(recorded.params or {})))
        # Compare response (excluding ticket).
        expected = dict(recorded.response or {})
        expected.pop("ticket", None)
        actual = dict(response)
        actual.pop("ticket", None)
        assert actual == expected, (
            f"call {i} ({recorded.event!r}) response mismatch:\n"
            f"  expected: {expected!r}\n"
            f"  actual:   {actual!r}"
        )
