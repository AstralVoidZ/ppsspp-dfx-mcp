"""Search-disassembly view — public JSON contract for ppsspp_search_disasm.

Exposes a `text` field alongside the structured fields. The text is a
multi-line disassembly listing, one line per result entry, prefixed
with the address.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.models.search_disasm import SearchDisasmResult
from ppsspp_dfx_mcp.views._base import FrozenModel


def _format_disasm_line(entry: dict[str, Any]) -> str:
    """Format a single disasm line.

    Accepts both 'text'-prefilled entries (PPSSPP returns these) and
    'name'/'params'-only entries (filled in by DebugClient.search_disasm).
    """
    addr = entry.get("address", 0)
    try:
        addr_int = int(addr) if not isinstance(addr, int) else addr
        addr_str = f"0x{addr_int:08X}"
    except (TypeError, ValueError):
        addr_str = str(addr)
    text = entry.get("text")
    if not text:
        name = entry.get("name", "")
        params = entry.get("params", "")
        text = f"{name} {params}".strip() if params else name
    return f"{addr_str}: {text}"


class SearchDisasmResponse(FrozenModel):
    """Response view for ppsspp_search_disasm."""

    address: str = Field(
        description="Starting address for the search, hex string (e.g. '0x08804000')."
    )
    match: str = Field(description="Case-insensitive substring matched.")
    end: str = Field(
        default="0x0",
        description="End address, hex string. '0x00000000' or equal to `address` means loop search.",
    )
    results: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of disasm line dicts (address / text / name / params).",
    )
    text: str = Field(
        default="",
        description=(
            "Unified multi-line text representation. Each line is '0x{ADDR:08X}: {text}'."
        ),
    )

    @classmethod
    def from_result(cls, result: SearchDisasmResult) -> SearchDisasmResponse:
        lines = [_format_disasm_line(entry) for entry in result.results]
        return cls(
            address=format_address(result.address),
            match=result.match,
            end=format_address(result.end),
            results=list(result.results),
            text="\n".join(lines),
        )
