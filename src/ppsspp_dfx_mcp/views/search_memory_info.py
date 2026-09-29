"""Memory info search view — public JSON contract for ppsspp_search_memory_info.

Wraps the `memory.info.search` PPSSPP WebSocket response. The view
exposes the typed `regions` list, the `count`, the raw response, and a
unified multi-line text representation.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address, format_address_fields
from ppsspp_dfx_mcp.models.search_memory_info import SearchMemoryInfoResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class SearchMemoryInfoResponse(FrozenModel):
    """Response view for ppsspp_search_memory_info."""

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
    def from_result(cls, result: SearchMemoryInfoResult) -> SearchMemoryInfoResponse:
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
                lines.append(f"{format_address(addr_int)}-{format_address(end_int)} {type_}{tag_str}")
            except (TypeError, ValueError):
                lines.append(f"{addr}-{size} {type_} {tag}")
        # Normalize all address-bearing fields (address, pc, etc.) in both
        # the structured regions and the raw response. Previously only
        # `address` was converted; `pc` and other address fields were left
        # as decimal ints — format_address_fields handles them uniformly.
        regions = format_address_fields(result.regions)
        normalized_raw = format_address_fields(dict(result.raw))
        return cls(
            regions=regions,
            count=result.count,
            raw=normalized_raw,
            text="\n".join(lines),
        )
