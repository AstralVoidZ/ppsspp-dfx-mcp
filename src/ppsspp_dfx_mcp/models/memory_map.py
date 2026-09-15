"""Memory-map domain model (frozen dataclass).

Wraps the result of `PpssppDebugClient.memory_map()` — a READ-ONLY
operation that calls `memory.mapping` (no parameters) and returns a
`ranges` array of memory regions (ram / vram / sram, primary / mirror).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MemoryMapResult:
    """Result of a memory map query.

    Attributes:
        mapping: Raw response from `memory.mapping` — a dict with a
            `ranges` array of region dicts (type / subtype / name /
            address / size).
    """

    mapping: dict[str, Any] = field(default_factory=dict)
