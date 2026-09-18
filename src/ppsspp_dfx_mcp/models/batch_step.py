"""Batch step orchestration domain models (frozen dataclass).

batch_step executes a sequence of primitive steps (press / wait /
state_probe / screenshot) in order, collecting per-step results.
Recording-mode aware: if the session is currently recording a replay
(saving=true), screenshot steps are rejected up-front:
gpu.buffer.screenshot requires stepping, which breaks
recording timing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, NotRequired, TypedDict, get_args, get_type_hints

from pydantic import Field

from ppsspp_dfx_mcp.core.primitives import MAX_PRESS_DURATION_FRAMES, MAX_WAIT_FRAMES
from ppsspp_dfx_mcp.models.input import PSPButton


# ── AI-usability round: typed step inputs ────────────────────────────────
# Discriminated-union TypedDicts: the MCP inputSchema gains per-type
# properties plus a 'type' discriminator, so models can't invent step
# shapes. Pydantic validates TypedDicts back to PLAIN DICTS (scratch-
# verified), so the batch handler keeps dict access and direct test
# calls with raw dicts are unaffected. Extra keys stay accepted.
class PressStep(TypedDict):
    """{type:'press', button, duration?} — one PSP button press."""

    type: Literal["press"]
    button: Annotated[
        PSPButton,
        Field(description="Button name (25-item whitelist, e.g. 'cross'/'start')."),
    ]
    duration: NotRequired[
        Annotated[
            int,
            Field(
                description=(
                    f"Press duration in frames (60fps, cap {MAX_PRESS_DURATION_FRAMES}); default 1."
                )
            ),
        ]
    ]


class WaitStep(TypedDict):
    """{type:'wait', frames} — idle wait."""

    type: Literal["wait"]
    frames: Annotated[
        int,
        Field(description=f"Frames to wait (60fps wall-clock; cap {MAX_WAIT_FRAMES})."),
    ]


class StateProbeStep(TypedDict):
    """{type:'state_probe', names?, samples?} — sample registered probes."""

    type: Literal["state_probe"]
    names: NotRequired[
        Annotated[
            str,
            Field(
                description=("Comma-separated probe names; omit to observe all registered probes."),
            ),
        ]
    ]
    samples: NotRequired[
        Annotated[
            int,
            Field(
                description=("Samples per probe (default 1); final value is the last read."),
            ),
        ]
    ]


class CpuStepStep(TypedDict):
    """{type:'cpu_step', mode, count} — N CPU instructions (into/over/out)."""

    type: Literal["cpu_step"]
    mode: Annotated[
        Literal["into", "over", "out"],
        Field(description="CPU stepping mode: 'into', 'over', or 'out'."),
    ]
    count: Annotated[
        int,
        Field(description="Number of CPU steps to execute (1..1000; default 1)."),
    ]


class ScreenshotStep(TypedDict):
    """{type:'screenshot', source?} — capture the framebuffer."""

    type: Literal["screenshot"]
    source: NotRequired[
        Annotated[
            Literal["render", "output"],
            Field(description="'render' (default) or 'output' (CRASH-RISK, do not use)."),
        ]
    ]


BatchStepInput = Annotated[
    PressStep | WaitStep | StateProbeStep | ScreenshotStep | CpuStepStep,
    Field(discriminator="type"),
]

# Authoritative step-type list — what tools/batch_step validates against.
# The four TypedDicts each pin their 'type' via a single-value Literal;
# the import-time assert below locks the two representations together.
STEP_TYPES: tuple[str, ...] = ("press", "wait", "state_probe", "screenshot", "cpu_step")

assert (
    tuple(get_args(get_type_hints(t)["type"])[0] for t in get_args(get_args(BatchStepInput)[0]))
    == STEP_TYPES
), "step-type Literals drifted from STEP_TYPES"


@dataclass(frozen=True)
class StepResult:
    """Result of a single step within a batch.

    Attributes:
        index: Step position in the batch (0-based).
        type: Step type that was executed ('press' / 'wait' /
            'state_probe' / 'screenshot').
        status: 'success' / 'failure' / 'skipped'.
        error: Empty on success; error message on failure.
        data: Step-type-specific result payload (e.g. button name for
            press, frames for wait, observations for state_probe,
            screenshot metadata for screenshot).
    """

    index: int
    type: str
    status: str = "success"
    error: str = ""
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class BatchResult:
    """Aggregate result of a batch_step execution.

    Attributes:
        action: Always 'run'.
        total: Total number of steps in the batch.
        executed: Number of steps actually executed (excludes skipped).
        succeeded: Number of steps with status='success'.
        failed: Number of steps with status='failure'.
        skipped: Number of steps with status='skipped'.
        recording_mode: Whether the session was in replay recording
            mode (saving=true) when the batch ran.
        results: Per-step results, in order.
        aborted: Whether the batch aborted early (a step with
            on_failure='abort' failed).
        abort_reason: Empty if not aborted; reason message if aborted.
    """

    action: str = "run"
    total: int = 0
    executed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    recording_mode: bool = False
    results: tuple[StepResult, ...] = field(default_factory=tuple)
    aborted: bool = False
    abort_reason: str = ""
