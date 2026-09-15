"""Evaluate domain model (frozen dataclass).

Wraps the result of `PpssppDebugClient.evaluate(expression)` — a
READ-ONLY evaluation of a debugger expression (e.g., `r5 + 0x10`) under
a `with_stepping` context so register values are trustworthy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EvaluateResult:
    """Result of a debugger expression evaluation.

    Attributes:
        expression: The expression string sent to PPSSPP.
        value: The evaluated value (int) if PPSSPP returned a numeric
            result; None if the response shape was unexpected.
        response: Raw PPSSPP WebSocket response dict.
    """

    expression: str = ""
    value: int | None = None
    response: dict[str, Any] | None = None
