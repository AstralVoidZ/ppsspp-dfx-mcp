"""Assemble view — public JSON contract for ppsspp_assemble.

Task 8.3 (unified output format): exposes a `text` field alongside the
structured fields, following the convention:
    'Assembled {N} bytes → 0x{ADDR:08X}'
(where N is the byte count if `response['bytes']` is a list, else 0).
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.models.assemble import AssembleResult
from ppsspp_dfx_mcp.views._base import FrozenModel


def _byte_count(response: dict[str, Any] | None) -> int:
    """Best-effort byte count from a memory.assemble response.

    PPSSPP's `memory.assemble` returns ``{"encoding": <u32>}`` where
    ``encoding`` is the assembled instruction's encoding value (not
    byte count). MIPS instructions are fixed 4 bytes, so when
    ``encoding`` is present we return 4 per instruction.

    When the tool wrapper assembles multiple instructions
    sequentially, it injects an ``instruction_count`` field into the
    response dict. If present, the byte count is
    ``instruction_count * 4``. Otherwise the single-instruction path
    returns 4.
    """
    if not response:
        return 0
    # Multi-instruction path injects instruction_count.
    instr_count = response.get("instruction_count")
    if isinstance(instr_count, int) and instr_count > 0:
        return instr_count * 4
    raw = response.get("bytes") or response.get("data") or response.get("encoded")
    if isinstance(raw, (bytes, bytearray)):
        return len(raw)
    if isinstance(raw, list):
        # List of instruction encodings — each is 4 bytes (MIPS fixed width).
        return len(raw) * 4
    # PPSSPP memory.assemble returns {"encoding": <u32>} — the assembled
    # instruction's encoding value. MIPS instructions are 4 bytes.
    if "encoding" in response and isinstance(response["encoding"], int):
        return 4
    return 0


class AssembleResponse(FrozenModel):
    """Response view for ppsspp_assemble."""

    address: str = Field(description="Target address, hex string (e.g. '0x08804000').")
    code: str = Field(description="Assembly source string passed in.")
    bytes_written: int = Field(
        default=0,
        description=(
            "Number of bytes written. Best-effort: derived from "
            "response['bytes'] length when available, else 0."
        ),
    )
    response: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw PPSSPP WebSocket response.",
    )
    text: str = Field(
        default="",
        description="Unified text representation: 'Assembled N bytes → 0x{ADDR:08X}'.",
    )

    @classmethod
    def from_result(cls, result: AssembleResult) -> AssembleResponse:
        n = _byte_count(result.response)
        text = f"Assembled {n} bytes → {format_address(result.address)}"
        return cls(
            address=format_address(result.address),
            code=result.code,
            bytes_written=n,
            response=dict(result.response),
            text=text,
        )
