"""Attribute a timed-out ticketed call to a cause.

This failure was misdiagnosed twice before it was found. Both times the tool said
only "timed out", which is compatible with at least four very different
situations:

  1. the CPU was stepping -> no frame is coming, so the wait is CORRECT
  2. a broadcast for the same event arrived meanwhile -> the response path
     is alive, so ticket pairing is what broke
  3. nothing at all arrived -> the producer is not running. This was the
     real cause: a modal "Graphics Error" dialog was blocking the
     emulator, so no GPU flip ever happened and nothing was pushed.
  4. some other failure (transport closed, decode error, ...)

A caller who cannot tell these apart has to guess, and guessing is what
produced two wrong root causes. These helpers read the CallDiagnostics
fields and name the case.

Pure functions over the record dicts, so they are testable without a
transport.
"""

from __future__ import annotations

from typing import Any

# Attribution outcomes, most specific first.
CAUSE_EXPECTED_STALL = "expected_stall"
CAUSE_PAIRING_BROKEN = "pairing_broken"
CAUSE_NO_PRODUCER = "no_producer"
CAUSE_TRANSPORT_ERROR = "transport_error"
CAUSE_UNKNOWN = "unknown"

_HINTS: dict[str, str] = {
    CAUSE_EXPECTED_STALL: (
        "the CPU was stepping, so no GPU frame is being produced; resume "
        "execution (or accept that ticketed GPU calls will wait) before "
        "retrying"
    ),
    CAUSE_PAIRING_BROKEN: (
        "a broadcast for the same event arrived while the call was pending, "
        "so the debugger is alive and the ticket pairing is what failed; "
        "this usually means the response went to a different waiter"
    ),
    CAUSE_NO_PRODUCER: (
        "nothing was broadcast for THIS EVENT during the whole wait. That is "
        "NOT the same as 'the emulator is not rendering': an experiment "
        "(2026-10-02, cube.iso) measured a 59.4 fps render with an animated "
        "screen while this event never answered. So do not conclude the game "
        "is stuck. The frame producer works; the producer for this event does "
        "not. Check whether PPSSPP emits this event at all on the current "
        "build (see mcp_test_report/gpu_channel_root_cause.md), and do not "
        "wait for a load that has already finished"
    ),
    CAUSE_TRANSPORT_ERROR: "the call failed for a reason other than a timeout",
    CAUSE_UNKNOWN: "the diagnostics did not carry enough to attribute this",
}


def attribute_timeout(record: dict[str, Any] | None) -> str:
    """Name the cause of a timed-out call from its diagnostic record.

    Returns one of the CAUSE_* constants. Never raises: an unusable record
    yields CAUSE_UNKNOWN rather than masking the original timeout.
    """
    if not record:
        return CAUSE_UNKNOWN

    # Stepping first: it is the one case where waiting was correct, so it
    # must win even if other fields are also set.
    if record.get("stepping_at_timeout") is True:
        return CAUSE_EXPECTED_STALL

    if not record.get("timed_out"):
        return CAUSE_UNKNOWN

    if record.get("last_same_event_broadcast_at") is not None:
        return CAUSE_PAIRING_BROKEN

    # error may hold any object at all. This runs on the failure path, so
    # it must not raise while trying to explain a failure.
    err = str(record.get("error") or "").lower()
    if err and any(k in err for k in ("closed", "disconnect", "decode", "json", "broken pipe")):
        return CAUSE_TRANSPORT_ERROR

    # Timed out with nothing broadcast at all: the producer never ran.
    return CAUSE_NO_PRODUCER


def hint_for(cause: str) -> str:
    """Actionable next step for an attribution outcome."""
    return _HINTS.get(cause, _HINTS[CAUSE_UNKNOWN])


def describe(record: dict[str, Any] | None) -> dict[str, Any]:
    """Full attribution for a record: cause plus what it was based on.

    The evidence fields are echoed back so a reader can check the reasoning
    instead of trusting it.
    """
    cause = attribute_timeout(record)
    return {
        "cause": cause,
        "hint": hint_for(cause),
        "evidence": {
            "timed_out": (record or {}).get("timed_out"),
            "stepping_at_timeout": (record or {}).get("stepping_at_timeout"),
            "saw_same_event_broadcast": (record or {}).get("last_same_event_broadcast_at")
            is not None,
            "elapsed_s": (record or {}).get("elapsed_s"),
            "error": (record or {}).get("error"),
        },
    }
