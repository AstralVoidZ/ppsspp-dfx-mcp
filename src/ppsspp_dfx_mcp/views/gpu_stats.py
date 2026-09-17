"""GPU stats view — public JSON contract for ppsspp_gpu_stats.

Wraps the `gpu.stats.get` PPSSPP WebSocket response. The view exposes
both typed fields (fps / vblanks_per_second) and raw dicts (info /
timing / raw) so clients can access fields the model doesn't model
explicitly.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.models.gpu_stats import GpuStatsResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class GpuStatsResponse(FrozenModel):
    """Response view for ppsspp_gpu_stats."""

    fps: float | None = Field(
        default=None,
        description=(
            "Frames per second. None if PPSSPP didn't return it (e.g., "
            "no game running, or response shape differs)."
        ),
    )
    vblanks_per_second: float | None = Field(
        default=None,
        description=("VBlanks per second. None if PPSSPP didn't return it."),
    )
    info: dict[str, Any] = Field(
        default_factory=dict,
        description="GPU info dict (vendor / name / version, etc.).",
    )
    timing: dict[str, Any] = Field(
        default_factory=dict,
        description="GPU timing dict (frame / block / vertex timing, etc.).",
    )
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw `gpu.stats.get` response from PPSSPP.",
    )
    text: str = Field(
        default="",
        description=(
            "Unified text representation: "
            "'fps={FPS} vblanks={VBLANKS} info_keys={N} timing_keys={N}'."
        ),
    )

    @classmethod
    def from_result(cls, result: GpuStatsResult) -> GpuStatsResponse:
        info_keys = len(result.info)
        timing_keys = len(result.timing)
        fps_str = f"{result.fps:.1f}" if (result.fps is not None) else "n/a"
        vblanks_str = (
            f"{result.vblanks_per_second:.1f}" if (result.vblanks_per_second is not None) else "n/a"
        )
        return cls(
            fps=result.fps,
            vblanks_per_second=result.vblanks_per_second,
            info=dict(result.info),
            timing=dict(result.timing),
            raw=dict(result.raw),
            text=(
                f"fps={fps_str} vblanks={vblanks_str} "
                f"info_keys={info_keys} timing_keys={timing_keys}"
            ),
        )
