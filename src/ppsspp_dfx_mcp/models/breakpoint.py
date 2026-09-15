"""Breakpoint domain models (frozen dataclass).

StepResult was split out to `models/step.py` (task 7.4) along with the
step() tool moving to `tools/step.py` (task 7.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BreakpointResult:
    """Result of a breakpoint operation.

    Attributes:
        action: 'set' / 'remove' / 'list' / 'update' / 'mem_set' /
            'mem_remove' / 'mem_list' / 'mem_update'.
        address: Breakpoint address (0 for list / mem_list).
        enabled: Whether the breakpoint is enabled (set / mem_set / update /
            mem_update actions only).
        breakpoints: List of breakpoint dicts (list / mem_list actions only).
    """

    action: str = "set"
    address: int = 0
    enabled: bool = True
    breakpoints: list[dict[str, Any]] = field(default_factory=list)
