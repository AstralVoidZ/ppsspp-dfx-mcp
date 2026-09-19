"""🔴-1/D1: MCP-side condition evaluator for CPU breakpoints.

PPSSPP v1.20.4 (IR mode) silently ignores register-referencing break
conditions — the condition is stored on the breakpoint but never
enforced, so "conditional" breakpoints fire unconditionally (live
verified 2026-09-20: s1==0x0000711 fired with s1=0x099F0250 while the
pure-constant 0==1 was correctly honoured).

Fix strategy: arm the breakpoint UNCONDITIONALLY in PPSSPP, keep the
condition in this registry, and enforce it MCP-side — on every hit,
evaluate the expression via PPSSPP's own ``cpu.evaluate`` (works while
stepping) and auto-resume when it's false. The caller only sees hits
whose condition held, plus a filtered-hit count.

The registry is intentionally dumb state: keyed by (session_id,
address), one entry per armed conditional breakpoint. Lifecycle is
owned by the breakpoint tool (register on set, drop on remove / storm
breaker / session stop).
"""

from __future__ import annotations

from typing import Any

# (session_id, address) -> {"condition": str, "filtered": int, "hits": int}
_filters: dict[tuple[str, int], dict[str, Any]] = {}


def register(session_id: str, address: int, condition: str) -> None:
    """Arm an MCP-side condition filter for an unconditional breakpoint."""
    _filters[(session_id, address)] = {
        "condition": condition,
        "filtered": 0,
        "hits": 0,
    }


def get(session_id: str, address: int) -> dict[str, Any] | None:
    """Return the filter entry for an address, or None."""
    return _filters.get((session_id, address))


def bump_hit(session_id: str, address: int) -> int:
    """Count a raw hit; returns the running hit count."""
    entry = _filters.get((session_id, address))
    if entry is None:
        return 0
    entry["hits"] += 1
    return entry["hits"]


def bump_filtered(session_id: str, address: int) -> int:
    """Count a filtered (condition-false) hit; returns the running count."""
    entry = _filters.get((session_id, address))
    if entry is None:
        return 0
    entry["filtered"] += 1
    return entry["filtered"]


def drop(session_id: str, address: int) -> None:
    """Drop the filter entry for one address."""
    _filters.pop((session_id, address), None)


def drop_session(session_id: str) -> int:
    """Drop every filter entry for a session (stop/GC hook). Returns count."""
    keys = [k for k in _filters if k[0] == session_id]
    for k in keys:
        del _filters[k]
    return len(keys)


def snapshot() -> dict[str, dict[str, Any]]:
    """Read-only copy for diagnostics/tests."""
    return {k: dict(v) for k, v in _filters.items()}
