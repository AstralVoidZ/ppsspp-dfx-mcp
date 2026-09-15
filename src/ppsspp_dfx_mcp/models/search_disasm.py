"""Search-disassembly domain model (frozen dataclass).

Wraps the result of `PpssppDebugClient.search_disasm(address, match,
end?)` — a READ-ONLY operation that calls PPSSPP's `memory.searchDisasm`
WebSocket event to find instructions whose disassembly text contains
`match` (case-insensitive substring).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SearchDisasmResult:
    """Result of a disassembly search.

    Attributes:
        address: Starting address for the search.
        match: Case-insensitive substring to match in disasm text.
        end: Optional end address (defaults to `address`, meaning a
            loop search).
        results: List of disasm line dicts returned by PPSSPP. Each
            entry typically has 'address' / 'text' / 'name' / 'params'.
    """

    address: int = 0
    match: str = ""
    end: int = 0
    results: list[dict[str, Any]] = field(default_factory=list)
