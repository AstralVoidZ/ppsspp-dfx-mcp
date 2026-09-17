"""Step view — public JSON contract for ppsspp_step.

Split from `views/breakpoint.py` (task 7.4) when the step() tool moved
to `tools/step.py` (task 7.3). The expanded action set covers step_into /
step_over / step_out / pause / resume / reset / run_until / next_hle.
"""

from __future__ import annotations

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.models.step import StepResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class StepResponse(FrozenModel):
    """Response view for ppsspp_step."""

    action: str = Field(
        description=(
            "'into' / 'over' / 'out' / 'pause' / 'resume' / 'reset' / 'run_until' / 'next_hle'."
        ),
    )
    address: str = Field(
        default="0x0",
        description="Target address for run_until, hex string (e.g. '0x08804000'); '0x00000000' for other actions.",
    )
    pc: str = Field(
        default="0x0",
        description=(
            "Program counter after step, hex string. For into/over/out/run_until/next_hle "
            "this comes from the cpu.stepping broadcast. For pause, extracted "
            "via safe_get_pc after CPU enters stepping. '0x00000000' for resume/reset "
            "(no trustworthy PC available)."
        ),
    )
    ticks: float = Field(
        default=0.0,
        description="CPU ticks at step completion (0.0 for pause/resume/reset).",
    )
    reason: str = Field(
        default="",
        description=(
            "Step reason from cpu.stepping broadcast (e.g. 'cpu.stepInto'). "
            "Empty for pause/resume/reset."
        ),
    )
    related_address: str = Field(
        default="0x0",
        description="Related address for temporary breakpoints, hex string. '0x00000000' when absent.",
    )

    @classmethod
    def from_result(cls, result: StepResult) -> StepResponse:
        return cls(
            action=result.action,
            address=format_address(result.address),
            pc=format_address(result.pc),
            ticks=result.ticks,
            reason=result.reason,
            related_address=format_address(result.related_address),
        )
