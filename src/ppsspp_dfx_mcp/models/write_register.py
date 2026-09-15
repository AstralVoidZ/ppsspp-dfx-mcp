"""Write-register domain model (frozen dataclass).

Wraps the result of `PpssppDebugClient.set_reg(name, value)` — a
DESTRUCTIVE operation that mutates CPU register state via the
`cpu.setReg` WebSocket event under a `with_stepping` context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WriteRegisterResult:
    """Result of a CPU register write.

    Attributes:
        name: Register name (e.g., 'v0', 'a0', 'pc', 'hi', 'lo').
        value: Value written (int).
        response: Raw PPSSPP WebSocket response dict (may be empty).
    """

    name: str = ""
    value: int = 0
    response: dict[str, Any] | None = None
