"""Assemble domain model (frozen dataclass).

Wraps the result of `PpssppDebugClient.assemble(address, code)` — a
DESTRUCTIVE operation that assembles MIPS instruction(s) and writes the
resulting bytes to memory at `address` via the `memory.assemble`
WebSocket event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AssembleResult:
    """Result of a MIPS assembly write.

    Attributes:
        address: Target address where assembled bytes were written.
        code: The assembly source string passed in (e.g., 'nop' or
            'addiu r5, r0, 0x10').
        response: Raw PPSSPP WebSocket response dict. May contain
            'bytes' (assembled bytes) or other encoder metadata.
    """

    address: int = 0
    code: str = ""
    response: dict[str, Any] = field(default_factory=dict)
