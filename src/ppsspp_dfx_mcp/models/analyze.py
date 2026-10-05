"""Analyze domain models (frozen dataclass)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LogMatch:
    """A single matched log line.

    Attributes:
        line_no: 1-based line number.
        text: Matched line text.
    """

    line_no: int = 0
    text: str = ""


@dataclass(frozen=True)
class AnalyzeLogResult:
    """Result of a log analysis.

    Attributes:
        log_path: Path to the log file (or '(default)' if from launcher).
        matches: List of matching log lines.
        count: Number of matches (after limit truncation, if any).
        filter: User-supplied keyword filter (empty string if none).
        filter_mode: 'any' (line matches severity keywords OR filter,
            legacy behavior) or 'all' (severity keywords AND filter).
        total_matches: Match count before `limit` truncation.
        truncated: True when limit truncated the match list.
    """

    log_path: str = "(default)"
    matches: list[LogMatch] = field(default_factory=list)
    count: int = 0
    filter: str = ""
    filter_mode: str = "any"
    total_matches: int = 0
    truncated: bool = False
