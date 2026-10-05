"""Memory-map view — public JSON contract for ppsspp_memory_map.

Wraps the `memory.mapping` PPSSPP WebSocket response. The `ranges`
field exposes the region list extracted from the response, while
`mapping` retains the raw response for clients that need the full
PPSSPP payload.

G-4 (FR-004): the per-call `ticket` PPSSPP stamps on every reply is a
protocol pairing artifact, not business data — keeping it made the
structured business fields differ on every call (t30→t39 across ten
identical calls), so whole-payload idempotence comparison could never
succeed. Volatile keys are stripped from `mapping`.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address, format_address_fields
from ppsspp_dfx_mcp.models.memory_map import MemoryMapResult
from ppsspp_dfx_mcp.views._base import FrozenModel

# Protocol fields PPSSPP injects per call that carry no business data.
# See module docstring (G-4). Keep the set explicit so adding a field is
# a deliberate act, not an oversight.
_VOLATILE_MAPPING_KEYS: frozenset[str] = frozenset({"ticket"})


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
        description=(
            "Raw `memory.mapping` response, minus the volatile per-call "
            "protocol `ticket` (stripped so the business fields are "
            "byte-stable across identical calls)."
        ),
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
                lines.append(
                    f"{format_address(start_int)}-{format_address(end_int)} {type_}/{subtype} {name}"
                )
            except (TypeError, ValueError):
                lines.append(f"{start}-{size} {type_}/{subtype} {name}")
        # Normalize address fields in the structured ranges and raw mapping
        # (PPSSPP returns decimal ints for address; format_address_fields
        # converts them to hex strings so the Agent never sees mixed formats
        # between the text rendering and the structured fields).
        normalized_ranges = format_address_fields(ranges)
        # G-4 (FR-004): strip the volatile ticket before normalization so
        # the remaining mapping is byte-stable across calls.
        stable_mapping = {
            k: v for k, v in result.mapping.items() if k not in _VOLATILE_MAPPING_KEYS
        }
        normalized_mapping = format_address_fields(stable_mapping)
        return cls(
            ranges=normalized_ranges,
            mapping=normalized_mapping,
            text="\n".join(lines),
        )
