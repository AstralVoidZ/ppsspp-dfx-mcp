"""Analyze view — public JSON contract for the analyze_log tool."""

from __future__ import annotations

from pydantic import Field

from ppsspp_dfx_mcp.models.analyze import AnalyzeLogResult
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
    count: int = Field(default=0, description="Number of matches (after limit truncation).")
    filter: str = Field(default="", description="User-supplied keyword filter.")
    filter_mode: str = Field(
        default="any",
        description="Filter combination mode: 'any' (legacy OR) or 'all' (severity AND filter).",
    )
    total_matches: int = Field(default=0, description="Match count before limit truncation.")
    truncated: bool = Field(default=False, description="True when limit truncated the match list.")

    @classmethod
    def from_result(cls, result: AnalyzeLogResult) -> AnalyzeLogResponse:
        return cls(
            log_path=result.log_path,
            matches=[LogMatchView(line_no=m.line_no, text=m.text) for m in result.matches],
            count=result.count,
            filter=result.filter,
            filter_mode=result.filter_mode,
            total_matches=result.total_matches,
            truncated=result.truncated,
        )
