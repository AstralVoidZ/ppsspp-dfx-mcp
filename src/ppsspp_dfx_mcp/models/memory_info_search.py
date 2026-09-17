"""Memory info search domain model (frozen dataclass).

Wraps the result of `PpssppDebugClient.memory_info_search()` — a READ-ONLY
operation that calls `memory.info.search` to query PPSSPP's memory
tracking system for allocation/write/texture metadata tags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _extract_regions(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract the regions list from a `memory.info.search` response.

    PPSSPP's `memory.info.search` (MemoryInfoSubscriber.cpp) returns a
    single `extent` dict (not a list) containing: type, address, size,
    ticks, pc, tag, allocated (field presence depends on PPSSPP version).
    This helper wraps the single dict into a one-element list for
    consistent downstream handling. Falls back to [] on unknown shapes.
    """
    val = raw.get("extent")
    if isinstance(val, dict):
        return [val]
    return []


@dataclass(frozen=True)
class MemoryInfoSearchResult:
    """Result of a `memory.info.search` query.

    Attributes:
        regions: List of matching memory region dicts. Each region has
            keys: type, address, size, ticks, pc, tag, allocated (field
            presence depends on PPSSPP version).
        count: Number of regions in the result.
        raw: Full raw response from `memory.info.search`.
    """

    regions: list[dict[str, Any]] = field(default_factory=list)
    count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> MemoryInfoSearchResult:
        """Build MemoryInfoSearchResult from a raw `memory.info.search` response."""
        regions = _extract_regions(raw)
        return cls(
            regions=regions,
            count=len(regions),
            raw=dict(raw),
        )
