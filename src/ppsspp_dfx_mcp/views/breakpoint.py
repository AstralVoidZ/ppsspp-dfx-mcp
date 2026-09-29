"""Breakpoint view — public JSON contract for ppsspp_breakpoint.

StepResponse was split out to `views/step.py` (task 7.4) along with the
step() tool moving to `tools/step.py` (task 7.3).
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address, format_address_fields
from ppsspp_dfx_mcp.models.breakpoint import BreakpointResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class BreakpointResponse(FrozenModel):
    """Response view for ppsspp_breakpoint."""

    action: str = Field(
        description=(
            "'set' / 'remove' / 'list' / 'update' / 'mem_set' / "
            "'mem_remove' / 'mem_list' / 'mem_update'."
        ),
    )
    address: str = Field(
        default="0x0",
        description="Breakpoint address, hex string (e.g. '0x08804000'); '0x00000000' for list / mem_list.",
    )
    enabled: bool = Field(
        default=True,
        description="Enabled flag (set / mem_set / update / mem_update only).",
    )
    breakpoints: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Breakpoint list (list / mem_list only).",
    )

    @classmethod
    def from_result(cls, result: BreakpointResult) -> BreakpointResponse:
        # Normalize address fields inside breakpoint list entries (PPSSPP
        # returns decimal ints for address; format_address_fields converts
        # them to hex strings so the Agent never sees mixed formats).
        normalized_breakpoints = format_address_fields(result.breakpoints)
        return cls(
            action=result.action,
            address=format_address(result.address),
            enabled=result.enabled,
            breakpoints=list(normalized_breakpoints),
        )
