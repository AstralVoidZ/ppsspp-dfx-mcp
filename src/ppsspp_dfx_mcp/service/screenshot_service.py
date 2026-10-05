"""Framebuffer-capture orchestration — extracted from ``tools/screenshot.py`` (W19).

``capture_frame`` owns the whole non-wire part of ``ppsspp_screenshot``:
session resolution, the legacy ``source``/``mode`` reconciliation, the
capture-strategy fallback chain, format/size probing and the metadata dict.
``tools/screenshot.py`` only assembles the MCP ``CallToolResult`` (pixels +
``structuredContent``) and ``tools/batch_step.py``'s screenshot step calls
this service directly — previously it called the decorated tool function,
which stacked the tool error translator on top of the batch's own handler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.session.client_helper import resolve_session_id, session_capture

logger = logging.getLogger(__name__)

# Sentinel for "mode not explicitly provided by caller".
_MODE_DEFAULT = "auto"


def _detect_format(data: bytes) -> str:
    """Detect image format from magic bytes. Returns 'png' or 'jpeg'."""
    if len(data) >= 4 and data[:4] == b"\x89PNG":
        return "png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    return "png"


def _image_dims(data: bytes) -> tuple[int, int]:
    """Extract (width, height) from a PNG or JPEG image header.

    PNG: parse IHDR chunk (offset 16/20, 4 bytes BE each).
    JPEG: scan SOF0/SOF2 marker for dimensions (variable offset).
    Returns (0, 0) on failure or unsupported format.
    """
    if not data:
        return (0, 0)
    # PNG: 8-byte signature + IHDR at offset 16/20.
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) < 24:
            return (0, 0)
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    # JPEG: scan markers for SOF0 (0xFFC0) or SOF2 (0xFFC2).
    if data[:3] == b"\xff\xd8\xff":
        return _jpeg_dims(data)
    return (0, 0)


def _jpeg_dims(data: bytes) -> tuple[int, int]:
    """Parse JPEG SOF0/SOF2 marker for dimensions.

    JPEG structure: FFD8 [marker FFXX length data]... SOF0/2 marker
    contains: precision(1) + height(2) + width(2) after the length.
    """
    i = 2  # Skip FFD8.
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        # SOF0 (0xC0), SOF2 (0xC2) — contain dimensions.
        if marker in (0xC0, 0xC2):
            # offset: marker(2) + length(2) + precision(1) + height(2) + width(2)
            height = int.from_bytes(data[i + 5 : i + 7], "big")
            width = int.from_bytes(data[i + 7 : i + 9], "big")
            return (width, height)
        # Skip non-SOF markers (length is 2 bytes BE after marker).
        if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8, 0xD9):
            i += 2  # Standalone marker, no length.
        elif marker == 0xDA:  # SOS — scan data follows, stop.
            break
        else:
            if i + 3 < len(data):
                length = int.from_bytes(data[i + 2 : i + 4], "big")
                i += 2 + length
            else:
                break
    return (0, 0)


def _resolve_capture_strategy(
    source: str | None, mode: str | None, mode_explicit: bool
) -> tuple[str, str]:
    """Decide which CaptureService path to take and what label to record.

    Returns (strategy, label) where:
    - strategy is the CaptureService method name
    - label is the value exposed in the response `mode` field

    Raises ToolError if both source and mode are explicitly set.
    """
    source_set = source is not None
    if source_set and mode_explicit:
        raise ArgsInvalid(
            "ambiguous: provide either `source` (new) or `mode` (deprecated), not both"
        )
    if source_set:
        if source not in ("render", "output"):
            # was assert — stripped under python -O.
            raise ArgsInvalid(f"invalid source={source!r}; expected 'render' or 'output'")
        return (source, source)
    if mode_explicit:
        logger.warning(
            "ppsspp_screenshot: `mode` parameter is deprecated; use `source` "
            "(Literal['render', 'output']) instead. Got mode=%r.",
            mode,
        )
    if mode not in ("auto", "wm_command", "printwindow", "vram"):
        raise ArgsInvalid(f"invalid mode={mode!r}")
    return (mode, mode)


@dataclass(frozen=True)
class FrameCapture:
    """One framebuffer capture: pixels + the labels/metadata the MCP layer needs."""

    data: bytes
    label: str
    source: str | None
    img_format: str
    width: int
    height: int

    def output_filename(self) -> str:
        """Timestamped, label-tagged filename for the auto-save."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        ext = "jpg" if self.img_format == "jpeg" else "png"
        return f"{ts}_{self.label}.{ext}"

    def meta(self, file_path: str) -> dict[str, Any]:
        """The `structuredContent` metadata dict for this capture."""
        return {
            "mode": self.label,
            "source": self.source,
            "file_path": file_path,
            "size_bytes": len(self.data),
            "width": self.width,
            "height": self.height,
            "format": self.img_format,
            # An empty capture previously surfaced only
            # implicitly (size_bytes=0, no ImageContent). Make it explicit so
            # agents can branch on failure without parsing heuristics — same
            # intent as dump_texture's CAPTURE_EMPTY error.
            "empty": not self.data,
        }


async def capture_frame(
    session_id: str | None,
    source: str | None,
    mode: str | None,
) -> FrameCapture:
    """Resolve the session, run the capture-strategy chain and describe the result.

    ``mode`` is the deprecated legacy parameter; ``mode_explicit`` is derived
    from whether the caller passed it (None means "not provided"), so a
    default-filled value never triggers the deprecation warning.
    """
    session_id = await resolve_session_id(session_id)
    mode_explicit = mode is not None
    if not mode_explicit and source is None:
        mode = _MODE_DEFAULT

    strategy, label = _resolve_capture_strategy(source, mode, mode_explicit)

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_screenshot",
            "session_id": session_id,
            "source": source,
            "mode": mode,
            "strategy": strategy,
        },
    )
    async with session_capture(session_id) as (_client, capture):
        if strategy == "render":
            data = await capture.screenshot(source="render")
            # render_color returns empty bytes when the
            # GPU framebuffer hasn't been rendered yet (e.g., title
            # screen, loading screen). Fallback to safe_screenshot
            # (vram path) so the caller still gets a visual.
            if not data:
                data = await capture.safe_screenshot()
                label = "render→vram_fallback"
                source = "render→vram_fallback"
        elif strategy == "output":
            data = await capture.screenshot(source="output")
        elif strategy == "auto":
            data = await capture.safe_screenshot()
        elif strategy == "wm_command":
            data = await capture._wm_command_screenshot()
        elif strategy == "printwindow":
            data = await capture._print_window_screenshot()
        else:  # vram
            data = await capture._vram_screenshot()

    img_format = _detect_format(data)
    w, h = _image_dims(data) if data else (0, 0)
    return FrameCapture(
        data=data,
        label=label,
        source=source,
        img_format=img_format,
        width=w,
        height=h,
    )


__all__ = ["FrameCapture", "capture_frame"]
