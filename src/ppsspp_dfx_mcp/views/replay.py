"""Replay view — public JSON contract for ppsspp_replay.

Mirrors models/replay.py:ReplayResult. Each field carries a
`Field(description=...)` for
the MCP schema (TDQS), and a `from_result` factory converts the domain
dataclass into the view.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.models.replay import ReplayResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class ReplayResponse(FrozenModel):
    """Response view for ppsspp_replay."""

    action: str = Field(
        description=(
            "Replay action executed: 'begin' / 'abort' / 'flush' / "
            "'execute' / 'status' / 'time_get' / 'time_set' / 'save' / "
            "'load' / 'wait_complete'."
        ),
    )
    executing: bool = Field(
        default=False,
        description=(
            "True if a replay is currently executing. Drives "
            "`wait_complete`'s exit condition (polls until False)."
        ),
    )
    saving: bool = Field(
        default=False,
        description=(
            "True if a replay recording is in progress. After `begin` → "
            "True; after `flush` or `abort` → False."
        ),
    )
    version: int = Field(
        default=0,
        description=(
            "Recording format version from `replay.flush` (currently 1). "
            "0 when the action does not return a version."
        ),
    )
    size: int = Field(
        default=0,
        description=(
            "Recording size in bytes from `replay.flush`. 0 when the action does not return a size."
        ),
    )
    base64: str = Field(
        default="",
        description=(
            "Base64-encoded recording payload from `replay.flush`, or the "
            "input payload passed to `replay.execute`. Empty string when "
            "the action does not carry a payload."
        ),
    )
    base_rtc: int = Field(
        default=0,
        description=(
            "Base RTC timestamp (seconds) from `replay.time.get` / "
            "`replay.time.set`. 0 when the action does not return it."
        ),
    )
    data: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Raw PPSSPP response dict (echoed for diagnostic / future "
            "field extraction). Empty dict when no additional fields."
        ),
    )
    wait_iterations: int = Field(
        default=0,
        description=(
            "Number of `replay.status` polls performed by `wait_complete` "
            "before exiting. 0 for non-wait actions."
        ),
    )

    @classmethod
    def from_result(cls, result: ReplayResult) -> ReplayResponse:
        return cls(
            action=result.action,
            executing=result.executing,
            saving=result.saving,
            version=result.version,
            size=result.size,
            base64=result.base64,
            base_rtc=result.base_rtc,
            data=dict(result.data) if result.data else {},
            wait_iterations=result.wait_iterations,
        )
