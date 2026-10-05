"""Shared memcheck (memory-breakpoint) lookup helpers.

The address finder used to exist as hand-maintained copies across
``tools/breakpoint.py`` and ``tools/workflows.py`` (the latter's docstring
self-describing as a "local copy ... kept tiny on purpose"). Its
size-matching semantics already drifted once in project history —
removing a memcheck with the CALLER's size silently fails when it differs
from the registered one — so a single implementation lives here and both
tool modules delegate (review-v4 A-17).

Service/core layers must not import from tools/ (layering tripwire), so
this stays a tool-layer leaf: pure functions over the mem_bp_list
response shape.
"""

from __future__ import annotations

from typing import Any


def entry_address(bp: dict[str, Any]) -> int | None:
    """Parse a memcheck/breakpoint list entry's address.

    Wire shape is an int on current builds, but hex-string shapes have
    appeared on variant builds — ``int(raw)`` raised ValueError there and
    the whole tool call degraded to INTERNAL. Returns None for entries
    with missing/unparseable addresses so they are skipped, not fatal.
    """
    raw = bp.get("address")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if not s:
        return None
    try:
        return int(s, 16) if s.startswith("0x") else int(s)
    except ValueError:
        return None


def find_mem_bp(
    list_resp: dict[str, Any], address: int, size: int | None = None
) -> dict[str, Any] | None:
    """Find a memory breakpoint by address in a mem_bp_list response.

    PPSSPP's memory.breakpoint.list returns ``{"breakpoints": [...]}``
    where each entry has ``address``, ``size``, ``read``, ``write``,
    ``change`` fields. Used by mem_update to fetch the current state
    for merging partial read/write/change updates, and by mem_remove /
    trace cleanup to resolve the *actual* size of the memcheck being
    deleted (removing with the caller's size alone silently fails).

    When ``size`` is given the match requires an exact size; otherwise
    the first breakpoint at ``address`` wins (PPSSPP matches memchecks
    by start+end pair, so an address hosts at most one). Entries with
    unparseable addresses are skipped (see :func:`entry_address`).
    """
    bps = list_resp.get("breakpoints", []) if isinstance(list_resp, dict) else []
    for bp in bps:
        if not isinstance(bp, dict):
            continue
        if entry_address(bp) != address:
            continue
        if size is None or int(bp.get("size", 0)) == size:
            return bp
    return None
