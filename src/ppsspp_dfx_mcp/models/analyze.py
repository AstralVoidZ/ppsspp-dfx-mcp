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


@dataclass(frozen=True)
class AddressConversionResult:
    """Result of an address conversion.

    Attributes:
        original: Input address.
        converted: Output address.
        mode: Conversion mode used ('ida_to_ppsspp' / 'ppsspp_to_ida').
        top_base_ppsspp: top.prx PPSSPP base address.
        top_base_ida: top.prx IDA base address.
    """

    original: int = 0
    converted: int = 0
    mode: str = "ida_to_ppsspp"
    top_base_ppsspp: int = 0x08804000
    top_base_ida: int = 0x00000000
