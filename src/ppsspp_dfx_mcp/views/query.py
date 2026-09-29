"""Query view — public JSON contract for query / get_pc tools.

Task 8.3 (unified output format): QueryResponse exposes a `text` field
populated when action='registers'. The text uses grouped headers
'── GPR ──' / '── FPU ──' / '── VFPU ──' followed by
'  {reg_name:<7} = 0x{value:08X}' lines, per
specs/tdqs-descriptions/spec.md.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address, format_address_fields
from ppsspp_dfx_mcp.models.query import GetPcResult, QueryResult
from ppsspp_dfx_mcp.views._base import FrozenModel


def _format_registers_text(data: Any) -> str:
    """Format cpu.getAllRegs response as grouped register text.

    Expected `data` shape (from PPSSPP WebSocket cpu.getAllRegs):
        {
            "categories": [
                {"name": "GPR",  "registerNames": [...], "uintValues": [...]},
                {"name": "FPU",  "registerNames": [...], "uintValues": [...]},
                {"name": "VFPU", "registerNames": [...], "uintValues": [...]},
            ]
        }

    Each category is rendered as:
        ── {NAME} ──
          {reg_name:<7} = 0x{value:08X}
          ...

    Returns empty string if data is not the expected shape.
    """
    if not isinstance(data, dict):
        return ""
    categories = data.get("categories")
    if not isinstance(categories, list):
        return ""
    lines: list[str] = []
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        name = cat.get("name", "?")
        names = cat.get("registerNames", []) or []
        vals = cat.get("uintValues", []) or []
        lines.append(f"── {name} ──")
        for i, reg_name in enumerate(names):
            val = vals[i] if i < len(vals) else 0
            try:
                val_int = int(val)
            except (TypeError, ValueError):
                val_int = 0
            lines.append(f"  {str(reg_name):<7} = {format_address(val_int)}")
    return "\n".join(lines)


def _format_query_text(action: str, data: Any) -> str:
    """Format the unified text representation of a query result.

    Only action='registers' has a defined text format (grouped register
    output). Other actions return an empty string — callers should rely
    on the structured `data` field for those.
    """
    if action == "registers":
        return _format_registers_text(data)
    return ""


class QueryResponse(FrozenModel):
    """Response view for ppsspp_query."""

    action: str = Field(
        description=(
            "'game_state' / 'registers' / 'backtrace' / 'threads' / "
            "'modules' / 'funcs' / 'func_scan' / 'func_add' / 'func_remove'."
        ),
    )
    data: Any = Field(default=None, description="Raw result payload.")
    trust_level: str | None = Field(
        default=None,
        description=(
            "Trust annotation (threads + pc only). Lowercase enum value: "
            "'high' (stepping-verified) / 'medium' / 'low'."
        ),
    )
    text: str = Field(
        default="",
        description=(
            "Unified text representation. Populated for action='registers' "
            "with grouped '── GPR ──' / '── FPU ──' / '── VFPU ──' headers "
            "and '  name = 0xVAL' lines. Empty for other actions (use the "
            "structured `data` field)."
        ),
    )

    @classmethod
    def from_result(cls, result: QueryResult) -> QueryResponse:
        # Normalize address-bearing int fields in PPSSPP raw responses to
        # hex strings. PPSSPP returns thread pc/entry, module/function
        # addresses as decimal ints (e.g. pc=143585524); format_address_fields
        # converts them to the hex convention used everywhere else, so the
        # Agent never sees mixed decimal/hex formats across actions.
        # registers' uintValues are NOT affected (field name not in whitelist).
        normalized_data = format_address_fields(result.data)
        return cls(
            action=result.action,
            data=normalized_data,
            trust_level=result.trust_level,
            text=_format_query_text(result.action, result.data),
        )


class GetPcResponse(FrozenModel):
    """Response view for ppsspp_get_pc."""

    pc: str = Field(description="Program counter value, hex string (e.g. '0x08804000').")
    trust_level: str = Field(
        description=(
            "Trust annotation. Lowercase enum value: 'high' (stepping-verified) / 'medium' / 'low'."
        ),
    )

    @classmethod
    def from_result(cls, result: GetPcResult) -> GetPcResponse:
        return cls(pc=format_address(result.pc), trust_level=result.trust_level)
