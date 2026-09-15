"""GPU stats domain model (frozen dataclass).

Wraps the result of `PpssppDebugClient.gpu_stats()` — a READ-ONLY
operation that calls `gpu.stats.get` (async ticketed: PPSSPP pushes
stats on the next GPU flip). The response contains fps, vblanksPerSecond,
info, and timing fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _to_float(value: Any) -> float | None:
    """Best-effort coerce a JSON number to float; None on failure.

    Handles: int, float, numeric str, and dict with 'actual' key
    (PPSSPP returns fps/vblanksPerSecond as {"actual": N, "target": M}).
    """
    if value is None:
        return None
    # PPSSPP returns fps/vblanksPerSecond as {"actual": N, "target": M}
    if isinstance(value, dict):
        value = value.get("actual")
        if value is None:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_dict(value: Any) -> dict[str, Any]:
    """Best-effort coerce to dict (shallow copy); empty dict on failure.

    PPSSPP returns `info` as a string (not a dict); wrap non-dict values
    under a 'text' key so the info isn't lost.
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value:
        return {"text": value}
    return {}


@dataclass(frozen=True)
class GpuStatsResult:
    """Result of a `gpu.stats.get` query.

    Attributes:
        fps: Frames per second (float). None if PPSSPP didn't return it.
        vblanks_per_second: VBlanks per second (float). None if absent.
        info: GPU info dict (e.g., vendor/name/version). Empty dict if absent.
        timing: GPU timing dict (e.g., frame/block/vertex timing). Empty dict
            if absent.
        raw: Full raw response from `gpu.stats.get`. Preserved so clients can
            access fields not explicitly modeled above.
    """

    fps: float | None = None
    vblanks_per_second: float | None = None
    info: dict[str, Any] = field(default_factory=dict)
    timing: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "GpuStatsResult":
        """Build GpuStatsResult from a raw `gpu.stats.get` response.

        PPSSPP returns `fps` and `vblanksPerSecond` as numeric fields and
        `info` / `timing` as nested dicts. All fields are optional; missing
        or malformed values are coerced to None / empty dict.
        """
        return cls(
            fps=_to_float(raw.get("fps")),
            vblanks_per_second=_to_float(raw.get("vblanksPerSecond")),
            info=_to_dict(raw.get("info")),
            timing=_to_dict(raw.get("timing")),
            raw=dict(raw),
        )
