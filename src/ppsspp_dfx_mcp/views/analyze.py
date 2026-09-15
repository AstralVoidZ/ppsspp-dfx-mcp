"""Analyze view — public JSON contract for analyze_log / convert_address tools."""

from __future__ import annotations

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.models.analyze import (
    AddressConversionResult,
    AnalyzeLogResult,
    LogMatch,
)
from ppsspp_dfx_mcp.views._base import FrozenModel


class LogMatchView(FrozenModel):
    """A single matched log line."""

    line_no: int = Field(description="1-based line number.")
    text: str = Field(description="Matched line text.")


class AnalyzeLogResponse(FrozenModel):
    """Response view for ppsspp_analyze_log."""

    log_path: str = Field(
        description="Path to the log file (or '(default)' if from launcher).",
    )
    matches: list[LogMatchView] = Field(
        default_factory=list,
        description="Matching log lines.",
    )
    count: int = Field(default=0, description="Number of matches.")
    filter: str = Field(default="", description="User-supplied keyword filter.")

    @classmethod
    def from_result(cls, result: AnalyzeLogResult) -> "AnalyzeLogResponse":
        return cls(
            log_path=result.log_path,
            matches=[
                LogMatchView(line_no=m.line_no, text=m.text) for m in result.matches
            ],
            count=result.count,
            filter=result.filter,
        )


class AddressConversionResponse(FrozenModel):
    """Response view for ppsspp_convert_address."""

    original: str = Field(description="Input address, hex string (e.g. '0x08804000').")
    converted: str = Field(description="Output address, hex string (e.g. '0x08804000').")
    mode: str = Field(description="Conversion mode used ('ida_to_ppsspp'/'ppsspp_to_ida').")
    top_base_ppsspp: str = Field(description="top.prx PPSSPP base address, hex string.")
    top_base_ida: str = Field(description="top.prx IDA base address, hex string.")

    @classmethod
    def from_result(cls, result: AddressConversionResult) -> "AddressConversionResponse":
        return cls(
            original=format_address(result.original),
            converted=format_address(result.converted),
            mode=result.mode,
            top_base_ppsspp=format_address(result.top_base_ppsspp),
            top_base_ida=format_address(result.top_base_ida),
        )
