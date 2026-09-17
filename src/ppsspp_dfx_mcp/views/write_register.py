"""Write-register view — public JSON contract for ppsspp_write_register.

Task 8.3 (unified output format): exposes a `text` field alongside the
structured fields, following the convention:
    'Wrote 0x{VAL:X} → {REG}'
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.models.write_register import WriteRegisterResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class WriteRegisterResponse(FrozenModel):
    """Response view for ppsspp_write_register."""

    name: str = Field(description="Register name (e.g., 'v0', 'a0', 'pc', 'hi', 'lo').")
    value: str = Field(description="Value written, hex string (e.g. '0x00000001').")
    response: dict[str, Any] | None = Field(
        default=None,
        description="Raw PPSSPP WebSocket response (may be empty).",
    )
    text: str = Field(
        default="",
        description="Unified text representation: 'Wrote 0x{VAL:X} → {REG}'.",
    )

    @classmethod
    def from_result(cls, result: WriteRegisterResult) -> WriteRegisterResponse:
        text = f"Wrote 0x{result.value:X} → {result.name}"
        return cls(
            name=result.name,
            value=format_address(result.value),
            response=result.response,
            text=text,
        )
