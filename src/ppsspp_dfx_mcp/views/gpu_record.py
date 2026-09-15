"""GPU record view — public JSON contract for ppsspp_gpu_record.

Wraps the `gpu.record.dump` PPSSPP WebSocket response. The view exposes
the dump `size_bytes` and `file_path` (after auto-save) plus the raw
metadata. The binary dump itself is NOT embedded in the JSON response
(it can be large); callers access it via the saved file.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.models.gpu_record import GpuRecordResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class GpuRecordResponse(FrozenModel):
    """Response view for ppsspp_gpu_record."""

    size_bytes: int = Field(
        default=0,
        description=(
            "Size of the decoded GE command dump in bytes. 0 if PPSSPP "
            "returned no data (e.g., no game running, or timeout)."
        ),
    )
    file_path: str = Field(
        default="",
        description=(
            "Local file path where the dump was auto-saved "
            "(.ppsspp-dfx/output/gpu_dumps/<timestamp>.dump). Empty if "
            "the dump was empty or save failed."
        ),
    )
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="Raw `gpu.record.dump` response metadata from PPSSPP.",
    )
    text: str = Field(
        default="",
        description=(
            "Unified text representation: "
            "'dumped {N} bytes → {file_path}'."
        ),
    )

    @classmethod
    def from_result(
        cls, result: GpuRecordResult, file_path: str = ""
    ) -> "GpuRecordResponse":
        """Build view from a GpuRecordResult and the saved file path.

        Args:
            result: The GpuRecordResult from `GpuRecordResult.from_raw()`.
            file_path: Path where the dump was auto-saved. Empty string
                if the dump was empty or save was skipped.
        """
        if result.size > 0 and file_path:
            text = f"dumped {result.size} bytes → {file_path}"
        elif result.size > 0:
            text = f"dumped {result.size} bytes (not saved)"
        else:
            text = "no dump data returned"
        # Strip binary payload fields from raw to avoid embedding
        # large base64 strings in the JSON response (contradicts the "NOT
        # embedded" guarantee in the module docstring). PPSSPP returns the
        # dump as a data URI in the `uri` field
        # (e.g. 'data:application/octet-stream;base64,...') which contains
        # the full base64-encoded payload — this is the primary source of
        # the 386KB bloat. Also strip legacy `base64` and `data` fields.
        raw_meta = {
            k: v for k, v in result.raw.items()
            if k not in ("base64", "data", "uri")
        }
        return cls(
            size_bytes=result.size,
            file_path=file_path,
            raw=raw_meta,
            text=text,
        )
