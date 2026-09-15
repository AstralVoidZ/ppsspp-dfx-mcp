"""Workflow domain models (frozen dataclass) — H1 breakpoint-wait tools.

These back the two H1 composite tools (2026-09-07):
- ppsspp_wait_breakpoint — block until a breakpoint hit (cpu.stepping
  broadcast), lock-free subscription (R16 PARTIAL_HOLD contract)
- ppsspp_trace_memory_access — arm a memory breakpoint, wait for the
  hit, capture the scene, clean up, and restore CPU state in one call
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WaitBreakpointResult:
    """Result of ``ppsspp_wait_breakpoint``.

    Attributes:
        hit: True when the CPU entered stepping (breakpoint hit — or it
            was already paused when the tool was called).
        already_paused: True when the CPU was found in stepping state at
            arm time (the hit happened before the tool call).
        pc: Program counter of the hit (from the cpu.stepping broadcast,
            or a high-trust safe read in the already-paused case).
        reason: Broadcast reason field (e.g. 'breakpoint' /
            'memory.breakpoint' / 'cpu.stepInto').
        related_address: Broadcast relatedAddress (memory breakpoints:
            the accessed address).
        ticks: CoreTiming tick count at the hit.
    """

    hit: bool = False
    already_paused: bool = False
    pc: int | None = None
    reason: str | None = None
    related_address: int | None = None
    ticks: float | None = None


@dataclass(frozen=True)
class TraceAccessResult:
    """Result of ``ppsspp_trace_memory_access``.

    Attributes:
        hit: True when at least one access was captured.
        already_paused: True when the CPU was already paused at arm time
            (nothing can hit while paused — no breakpoint was armed).
        hits: Captured hits; each entry has pc / related_address /
            reason / ticks, plus 'registers' and/or 'backtrace' when
            requested.
        bp_removed: True when the tool's memory breakpoint is confirmed
            gone from PPSSPP's breakpoint table (list-verified).
        resumed: True when the tool resumed the CPU it had seen running
            at arm time (the hit pauses the CPU; the tool restores it).
        note: Optional human context (foreign-broadcast / partial
            cleanup caveats).
    """

    hit: bool = False
    already_paused: bool = False
    hits: list[dict[str, Any]] = field(default_factory=list)
    bp_removed: bool = False
    resumed: bool = False
    note: str | None = None


@dataclass(frozen=True)
class FrameSnapshotResult:
    """Result of ``ppsspp_frame_snapshot`` (P4, H2).

    Attributes:
        was_stepping: True when the CPU was already paused at entry.
        resumed: True when the tool resumed a CPU IT had paused.
        pc: High-trust program counter (CPU is stepping during capture).
        trust_level: safe_get_pc trust ('high').
        registers: Full GPR/FPU/VFPU dump when requested, else None.
        probes: state_observer capture block when requested, else None.
    """

    was_stepping: bool = False
    resumed: bool = False
    pc: int = 0
    trust_level: str = "high"
    registers: dict[str, Any] | None = None
    probes: dict[str, Any] | None = None
