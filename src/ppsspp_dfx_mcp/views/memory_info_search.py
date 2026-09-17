"""Memory info search view — public JSON contract for ppsspp_memory_info_search.

Wraps the `memory.info.search` PPSSPP WebSocket response. The view
exposes the typed `regions` list, the `count`, the raw response, and a
unified multi-line text representation.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.models.memory_info_search import MemoryInfoSearchResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class MemoryInfoSearchResponse(FrozenModel):
    """Response view for ppsspp_memory_info_search."""

    regions: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Matching memory regions. Each entry has type / address / size "
            "/ ticks / pc / tag / allocated (field presence depends on "
            "PPSSPP version)."
        ),
    )
    count: int = Field(
        default=0,
        description="Number of regions in the result.",
    )
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw `memory.info.search` response from PPSSPP.",
    )
    text: str = Field(
        default="",
        description=(
            "Unified text representation: one line per region, formatted "
            "as '0x{ADDR:08X}-0x{END:08X} {TYPE} {TAG}'."
        ),
    )

    @classmethod
    def from_result(cls, result: MemoryInfoSearchResult) -> MemoryInfoSearchResponse:
        lines: list[str] = []
        for region in result.regions:
            addr = region.get("address", 0)
            size = region.get("size", 0)
            type_ = region.get("type", "?")
            tag = region.get("tag", "")
            try:
                addr_int = int(addr) if not isinstance(addr, int) else addr
                size_int = int(size) if not isinstance(size, int) else size
                end_int = addr_int + size_int
                tag_str = f" {tag}" if tag else ""
                lines.append(f"0x{addr_int:08X}-0x{end_int:08X} {type_}{tag_str}")
            except (TypeError, ValueError):
                lines.append(f"{addr}-{size} {type_} {tag}")
        return cls(
            regions=list(result.regions),
            count=result.count,
            raw=dict(result.raw),
            text="\n".join(lines),
        )
