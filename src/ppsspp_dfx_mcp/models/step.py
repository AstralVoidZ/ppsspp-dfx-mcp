"""Step domain models (frozen dataclass).

Split from `models/breakpoint.py` (task 7.4) when the step() tool moved
to `tools/step.py` (task 7.3). The expanded action set covers step_into /
step_over / step_out / pause / resume / reset / run_until / next_hle.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StepResult:
    """Result of a CPU step / pause / resume / reset / run_until / next_hle.

    Attributes:
        action: 'into' / 'over' / 'out' / 'pause' / 'resume' / 'reset' /
            'run_until' / 'next_hle'.
        address: Target address for run_until (0 for other actions).
        pc: Program counter after step. For into/over/out/run_until/next_hle
            this comes from the cpu.stepping broadcast. For pause, extracted
            via cpu.getAllRegs after the CPU enters stepping (trustworthy
            because CPU is paused — see CPUCoreSubscriber.cpp:105). 0 for
            resume/reset (no trustworthy PC available).
        ticks: CPU ticks at step completion (from cpu.stepping broadcast).
            0.0 for pause/resume/reset.
        reason: Step reason string (from cpu.stepping broadcast, e.g.
            'cpu.stepInto'). Empty for pause/resume/reset.
        related_address: Related address for run_until/step_over temporary
            breakpoints (from cpu.stepping broadcast). 0 when absent.
    """

    action: str = "into"
    address: int = 0
    pc: int = 0
    ticks: float = 0.0
    reason: str = ""
    related_address: int = 0
