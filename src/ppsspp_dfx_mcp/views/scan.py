"""Scan view — public JSON contract for ppsspp_scan."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.views._base import FrozenModel


class StringHitView(FrozenModel):
    """One harvested string."""

    address: str = Field(description="String start address (hex).")
    text: str = Field(description="Decoded string.")


class ScanResponse(FrozenModel):
    """Union response view for ppsspp_scan (all modes / phases)."""

    mode: str = Field(description="Scan mode: pattern / value / strings.")
    # pattern
    action: str = Field(default="", description="Always 'scan' (pattern mode).")
    address: str = Field(default="", description="Scan start (pattern mode).")
    value: list[dict[str, Any]] = Field(default_factory=list, description="Matches (pattern mode).")
    size: int = Field(default=0, description="Match count (pattern mode).")
    # value
    scan_handle: str = Field(default="", description="Value-scan session handle.")
    width: str = Field(default="", description="Value width (u8/u16/u32).")
    candidates: int = Field(default=0, description="Candidate count (value initial/narrow).")
    passes: int = Field(default=0, description="Completed passes (value narrow).")
    addresses: list[str] = Field(
        default_factory=list, description="Candidate addresses (value list)."
    )
    dropped: bool = Field(default=False, description="True when the session was dropped.")
    # strings
    charset: str = Field(default="", description="Charset used (strings mode).")
    count: int = Field(default=0, description="Hit count (pattern & strings modes; matches the value/strings list in this response).")
    strings: list[StringHitView] = Field(
        default_factory=list, description="Harvested strings (strings mode)."
    )

    @classmethod
    def build_pattern(cls, start: int, matches: list[dict[str, Any]]) -> ScanResponse:
        return cls(
            mode="pattern",
            action="scan",
            address=format_address(start),
            value=matches,
            size=len(matches),
            # M13: `count` used to stay 0 in pattern mode (it was
            # strings-only), which read as "no matches" to callers
            # keying on count. Populate it for both modes.
            count=len(matches),
        )

    @classmethod
    def build_value_initial(cls, handle: str, width: str, candidates: int) -> ScanResponse:
        return cls(
            mode="value",
            scan_handle=handle,
            width=width,
            candidates=candidates,
            passes=1,
        )

    @classmethod
    def build_value_narrow(cls, handle: str, candidates: int, passes: int) -> ScanResponse:
        return cls(
            mode="value",
            scan_handle=handle,
            candidates=candidates,
            passes=passes,
        )

    @classmethod
    def build_value_list(cls, handle: str, addresses: list[int], width: str) -> ScanResponse:
        return cls(
            mode="value",
            scan_handle=handle,
            width=width,
            addresses=[format_address(a) for a in addresses],
            candidates=len(addresses),
        )

    @classmethod
    def build_value_drop(cls, handle: str) -> ScanResponse:
        return cls(mode="value", scan_handle=handle, dropped=True)

    @classmethod
    def build_strings(cls, charset: str, strings: list[dict[str, Any]]) -> ScanResponse:
        return cls(
            mode="strings",
            charset=charset,
            count=len(strings),
            strings=[
                StringHitView(address=format_address(s["address"]), text=s["text"]) for s in strings
            ],
        )


__all__ = ["ScanResponse", "StringHitView"]
