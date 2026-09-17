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
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from pydantic import Field

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
            Field(description="Press duration in frames (60fps, cap 18000); default 1."),
        ]
    ]


class WaitStep(TypedDict):
    """{type:'wait', frames} — idle wait."""

    type: Literal["wait"]
    frames: Annotated[
        int,
        Field(description="Frames to wait (60fps wall-clock; cap 18000)."),
    ]


class StateProbeStep(TypedDict):
    """{type:'state_probe', names?, samples?} — sample registered probes."""

    type: Literal["state_probe"]
    names: NotRequired[
        Annotated[
            str,
            Field(
                description=(
                    "Comma-separated probe names; omit to observe all "
                    "registered probes."
                ),
            ),
        ]
    ]
    samples: NotRequired[
        Annotated[
            int,
            Field(
                description=(
                    "Samples per probe (default 1); final value is the "
                    "last read."
                ),
            ),
        ]
    ]


class ScreenshotStep(TypedDict):
    """{type:'screenshot', source?} — capture the framebuffer."""

    type: Literal["screenshot"]
    source: NotRequired[
        Annotated[
            Literal["render", "output"],
            Field(
                description="'render' (default) or 'output' (CRASH-RISK, do not use)."
            ),
        ]
    ]


BatchStepInput = Annotated[
    PressStep | WaitStep | StateProbeStep | ScreenshotStep,
    Field(discriminator="type"),
]


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
