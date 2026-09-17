"""Query domain models (frozen dataclass)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class QueryResult:
    """Result of an aggregate query.

    Attributes:
        action: 'game_state' / 'registers' / 'backtrace' / 'threads'.
        data: Raw result payload (dict or list).
        trust_level: Lowercase enum value 'high'/'medium'/'low' (threads + pc only).
    """

    action: str = "game_state"
    data: Any = None
    trust_level: str | None = None


@dataclass(frozen=True)
class GetPcResult:
    """Result of safe_get_pc.

    Attributes:
        pc: Program counter value.
        trust_level: Lowercase enum value 'high' (stepping-verified) / 'medium' / 'low'.
    """

    pc: int = 0
    trust_level: str = "high"
