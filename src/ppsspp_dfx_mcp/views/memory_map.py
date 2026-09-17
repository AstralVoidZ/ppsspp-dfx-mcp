"""Memory-map view — public JSON contract for ppsspp_memory_map.

Wraps the `memory.mapping` PPSSPP WebSocket response. The `ranges`
field exposes the region list extracted from the response, while
`mapping` retains the raw response for clients that need the full
PPSSPP payload.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.models.memory_map import MemoryMapResult
from ppsspp_dfx_mcp.views._base import FrozenModel


def _extract_ranges(mapping: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort range list extraction from a memory.mapping response.

    PPSSPP returns a `ranges` array of region dicts (type / subtype /
    name / address / size). Fall back to [] on unknown shapes.
    """
    if not isinstance(mapping, dict):
        return []
    val = mapping.get("ranges")
    if isinstance(val, list):
        return val
    return []


class MemoryMapResponse(FrozenModel):
    """Response view for ppsspp_memory_map."""

    ranges: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Memory ranges from `memory.mapping`. Each entry has "
            "'type' (ram/vram/sram), 'subtype' (primary/mirror), "
            "'name', 'address', and 'size'."
        ),
    )
    mapping: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw `memory.mapping` response.",
    )
    text: str = Field(
        default="",
        description=(
            "Unified text representation: one line per range, formatted "
            "as '0x{ADDR:08X}-0x{END:08X} {TYPE}/{subtype} {NAME}'."
        ),
    )

    @classmethod
    def from_result(cls, result: MemoryMapResult) -> MemoryMapResponse:
        ranges = _extract_ranges(result.mapping)
        lines: list[str] = []
        for rng in ranges:
            name = rng.get("name", "?")
            type_ = rng.get("type", "?")
            subtype = rng.get("subtype", "?")
            start = rng.get("address", 0)
            size = rng.get("size", 0)
            try:
                start_int = int(start) if not isinstance(start, int) else start
                size_int = int(size) if not isinstance(size, int) else size
                end_int = start_int + size_int
                lines.append(f"0x{start_int:08X}-0x{end_int:08X} {type_}/{subtype} {name}")
            except (TypeError, ValueError):
                lines.append(f"{start}-{size} {type_}/{subtype} {name}")
        return cls(
            ranges=ranges,
            mapping=dict(result.mapping),
            text="\n".join(lines),
        )
