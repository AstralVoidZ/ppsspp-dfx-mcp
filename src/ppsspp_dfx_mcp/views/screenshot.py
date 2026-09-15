"""Screenshot view — public JSON contract for screenshot / dump_texture tools."""

from __future__ import annotations

import base64

from pydantic import Field

from ppsspp_dfx_mcp.models.screenshot import ScreenshotResult, TextureDumpResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class ScreenshotResponse(FrozenModel):
    """Response view for ppsspp_screenshot."""

    mode: str = Field(
        description=(
            "Capture mode used. Echoes the caller's `mode` value "
            "('auto'/'wm_command'/'printwindow'/'vram') on the deprecated "
            "path, or the `source` value ('render'/'output') on the new "
            "path."
        )
    )
    source: str | None = Field(
        default=None,
        description=(
            "The `source` parameter value when the new path was taken, or "
            "None when the deprecated `mode` path was used."
        ),
    )
    image_base64: str = Field(
        default="",
        description="base64-encoded image bytes (empty on failure).",
    )
    size_bytes: int = Field(default=0, description="Decoded image size in bytes.")
    width: int = Field(default=0, description="Image width in pixels (0 if unknown).")
    height: int = Field(default=0, description="Image height in pixels (0 if unknown).")
    file_path: str = Field(default="", description="Path where image was saved.")
    format: str = Field(default="png", description="Image format ('png' or 'jpeg').")
    empty: bool = Field(
        default=False,
        description=(
            "True when the capture produced no pixels (size_bytes=0 and no "
            "ImageContent). Agents can branch on this instead of parsing "
            "size_bytes heuristics — mirrors dump_texture's CAPTURE_EMPTY error."
        ),
    )

    @classmethod
    def from_result(cls, result: ScreenshotResult) -> "ScreenshotResponse":
        b64 = result.image_base64
        if not b64 and result.image_data:
            b64 = base64.b64encode(result.image_data).decode("ascii")
        return cls(
            mode=result.mode,
            source=result.source,
            image_base64=b64,
            size_bytes=result.size_bytes,
            width=result.width,
            height=result.height,
            file_path=result.file_path,
            format=result.format,
            # S2: `empty` 必须由工厂一并设置——它是「本次捕获没有像素」的**显式**
            # 信号，漏掉会让 0 字节捕获报成 empty=False，正是该字段要消除的歧义。
            # （工具路径手写 meta 时已正确设置；工厂是另一条入口。）
            empty=result.size_bytes == 0,
        )


class TextureDumpResponse(FrozenModel):
    """Response view for ppsspp_dump_texture."""

    level: int = Field(default=0, description="Texture mipmap level dumped.")
    image_base64: str = Field(
        default="",
        description="base64-encoded image bytes (empty on failure).",
    )
    size_bytes: int = Field(default=0, description="Decoded image size in bytes.")
    file_path: str = Field(default="", description="Path where image was saved.")
    format: str = Field(default="png", description="Image format ('png' or 'jpeg').")

    @classmethod
    def from_result(cls, result: TextureDumpResult) -> "TextureDumpResponse":
        b64 = result.image_base64
        if not b64 and result.image_data:
            b64 = base64.b64encode(result.image_data).decode("ascii")
        return cls(
            level=result.level,
            image_base64=b64,
            size_bytes=result.size_bytes,
            file_path=result.file_path,
            format=result.format,
        )
