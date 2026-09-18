"""Scan tool models — value-scan session state for ppsspp_scan(mode="value")."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ValueScanSession:
    """A narrowing session: candidate addresses + scan parameters."""

    width: str  # "u8" | "u16" | "u32"
    addresses: list[int] = field(default_factory=list)
    created_at: float = 0.0
    passes: int = 0
