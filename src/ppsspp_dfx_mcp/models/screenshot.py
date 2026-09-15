"""Screenshot domain models (frozen dataclass)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScreenshotResult:
    """Result of a screenshot capture.

    Attributes:
        mode: Capture mode used.
        source: The `source` parameter value, or None for legacy path.
        image_base64: base64-encoded image bytes (populated by caller or
            computed from image_data in view layer).
        image_data: Raw image bytes (empty on failure).
        file_path: Path where image was saved (empty if not saved).
        format: Image format ("png" or "jpeg").
        size_bytes: Decoded image size in bytes.
        width: Image width in pixels (0 if unknown).
        height: Image height in pixels (0 if unknown).
    """

    mode: str = "auto"
    source: str | None = None
    image_base64: str = ""
    image_data: bytes = b""
    file_path: str = ""
    format: str = "png"
    size_bytes: int = 0
    width: int = 0
    height: int = 0


@dataclass(frozen=True)
class TextureDumpResult:
    """Result of a GPU texture dump.

    Attributes:
        level: Texture mipmap level dumped (PPSSPP captures the
            currently-bound texture — address/texfmt/width/height are
            NOT supported by the gpu.buffer.texture protocol).
        image_base64: base64-encoded image bytes (populated by caller or
            computed from image_data in view layer).
        image_data: Raw image bytes (empty on failure).
        file_path: Path where image was saved (empty if not saved).
        format: Image format ("png" or "jpeg").
        size_bytes: Decoded image size in bytes.
    """

    level: int = 0
    image_base64: str = ""
    image_data: bytes = b""
    file_path: str = ""
    format: str = "png"
    size_bytes: int = 0
