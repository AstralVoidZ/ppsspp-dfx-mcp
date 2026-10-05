"""Screenshot tool wrappers.

2 tools exposed:
- ppsspp_screenshot(session_id, source?, mode?) — capture PPSSPP framebuffer
- ppsspp_dump_texture(session_id, level?) — dump currently-bound GPU texture

Returns [TextContent(metadata_json), ImageContent(image)] for token efficiency.
FastMCP _convert_to_content() recursively flattens lists: str→TextContent,
Image→ImageContent. This reduces token cost from ~46K (base64 as text) to
~220 (visual token + metadata) per screenshot.

Task 11.5 (parameter deprecation): `source` is the new preferred
parameter; `mode` is deprecated.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypedDict

from mcp.server.mcpserver import Image
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.errors import ArgsInvalid, CaptureEmpty
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.service.screenshot_service import (
    _image_dims,  # noqa: F401 — re-exported for image-header tests
    capture_frame,
)
from ppsspp_dfx_mcp.session.client_helper import session_capture
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import save_output_bytes, translate_tool_errors
from ppsspp_dfx_mcp.views.screenshot import ScreenshotResponse, TextureDumpResponse

logger = logging.getLogger(__name__)

# 图像工具的元数据契约（openspec `tool-schema-contract` + `imagecontent-output`）。
#
# 三者都用 `Annotated[CallToolResult, <契约>]` 作返回标注：像素数据经 `content`
# 的 `ImageContent` 传递，`structuredContent` 只承载元数据。SDK 显式支持该组合
# （`func_metadata.py:416-420`）——从 `Annotated` 的 metadata 取输出模型推导
# schema，同时保留工具自建 `CallToolResult`（含图像块）的能力。
#
# `image_base64` 被 **exclude**：它是 `ImageContent` 的另一种编码，进入结构化
# 通道既膨胀 schema 又与"图像走 content"的契约冲突。
ScreenshotMeta = derive_output_contract(
    "ScreenshotMeta", ScreenshotResponse, exclude=frozenset({"image_base64"})
)
# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    TextureDumpMeta = dict[str, Any]
else:
    TextureDumpMeta = derive_output_contract(
        "TextureDumpMeta", TextureDumpResponse, exclude=frozenset({"image_base64"})
    )


class ClutDumpMeta(TypedDict):
    """`ppsspp_dump_clut` 的元数据契约。

    `dump_clut` 没有对应的 Pydantic view（`views/screenshot.py` 只覆盖
    screenshot / dump_texture），故手工声明；字段与下文构造的 `meta` 一致。
    """

    file_path: str
    size_bytes: int
    format: str


__all__ = ["screenshot", "dump"]


async def _save_to_output(data: bytes, subdir: str, filename: str) -> str:
    """Save data to .ppsspp-dfx/output/{subdir}/{filename}. Returns file path.

    P2 refactor: delegates to the shared containment + to_thread helper
    — multi-MB PNG writes no longer stall the event loop.
    """
    return await save_output_bytes(subdir, filename, data)


@mcp.tool(
    name="ppsspp_screenshot",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
@translate_tool_errors
async def screenshot(
    session_id: Annotated[
        str | None,
        Field(
            description=(
                "Active session ID; omit to auto-resolve when exactly one session is active."
            ),
        ),
    ] = None,
    source: Annotated[
        Literal["render", "output"] | None,
        Field(
            default=None,
            description=(
                "Capture source (new, preferred). 'render' = with_stepping + "
                "gpu.buffer.renderColor (default when neither source nor mode "
                "is given). 'output' = gpu.buffer.screenshot (CRASH-RISK on "
                "some games). Mutually exclusive with `mode`."
            ),
        ),
    ] = None,
    mode: Annotated[
        Literal["auto", "wm_command", "printwindow", "vram"] | None,
        Field(
            default=None,
            description=(
                "DEPRECATED — use `source` instead. Legacy Win32 / VRAM "
                "fallback paths. 'auto' = three-tier fallback "
                "(wm_command → printwindow → vram). 'wm_command' / "
                "'printwindow' / 'vram' = the specific strategy. Mutually "
                "exclusive with `source`."
            ),
        ),
    ] = None,
) -> Annotated[CallToolResult, ScreenshotMeta]:
    """PURPOSE: Capture the framebuffer as an image (ImageContent) plus metadata.

    USAGE: session_id optional when exactly one session is active; source='render' (default; empty frames auto-fall back to VRAM — colors unreliable there) or 'output' (CRASH-RISK, do not use); mutually exclusive with the deprecated mode param.

    BEHAVIOR: READ-ONLY. An empty capture returns empty=true instead of an error — advance to a rendered scene and retry.

    RETURNS: structuredContent metadata (mode/source/size_bytes/width/height/file_path/format/empty); the image itself arrives as an ImageContent block. The auto-saved PNG/JPG path is in file_path."""
    # The capture orchestration (session resolve + source/mode reconciliation
    # + strategy fallback + format/size probing) lives in
    # `service/screenshot_service.py`; this tool only assembles the MCP result.
    frame = await capture_frame(session_id, source, mode)

    file_path = ""
    if frame.data:
        file_path = await _save_to_output(frame.data, "screenshots", frame.output_filename())

    meta = frame.meta(file_path)

    # ImageContent carries the pixels; structuredContent carries the metadata.
    # An empty capture yields metadata only (empty=true), not an error.
    # NOTE: `Image` is a b64 helper, not a content model — it must be converted
    # via `to_image_content()` before entering a hand-built CallToolResult
    # (the SDK only does that conversion on the `convert_result` path, which
    # building the result ourselves bypasses).
    content: list[Any] = []
    if frame.data:
        content.append(Image(data=frame.data, format=frame.img_format).to_image_content())
    return CallToolResult(content=content, structured_content=meta)


@mcp.tool(
    name="ppsspp_dump",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
@translate_tool_errors
async def dump(
    kind: Annotated[
        Literal["texture", "clut"],
        Field(
            description=(
                "What to capture from the CURRENTLY bound GPU state "
                "(no VRAM-address targeting): 'texture' = the bound "
                "texture (use level for mipmap); 'clut' = the bound "
                "CLUT palette."
            ),
        ),
    ],
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    level: Annotated[
        int,
        Field(
            default=0,
            description=(
                "Texture mipmap level (default 0). kind=texture only — "
                "a non-zero level with kind=clut is rejected."
            ),
        ),
    ] = 0,
) -> Annotated[CallToolResult, TextureDumpMeta]:
    """PURPOSE: Dump the currently-bound GPU texture OR CLUT palette as an image plus metadata.

    USAGE: kind='texture' → the bound texture (level selects mipmap; PPSSPP captures the currently-bound texture — it does NOT support capture by VRAM address); kind='clut' → the bound palette (level must be 0).

    BEHAVIOR: READ-ONLY. An empty capture raises CAPTURE_EMPTY — enter a scene that renders (texture) or uses the palette (clut) and retry.

    RETURNS: structuredContent metadata (kind/level/file_path/size_bytes/format); the image itself arrives as an ImageContent block."""
    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_dump", "kind": kind, "session_id": session_id, "level": level},
    )
    if kind == "clut" and level != 0:
        raise ArgsInvalid("level applies only to kind='texture'; got kind='clut'")
    async with session_capture(session_id) as (_client, capture):
        if kind == "clut":
            data = await capture.dump_clut()
        else:
            data = await capture.dump_texture(level=level)
    # An empty capture means PPSSPP could not deliver the payload (nothing
    # bound at this state) — surface it as an error instead of a success
    # with size_bytes=0.
    if not data:
        what = "CLUT" if kind == "clut" else "texture"
        # typed exception carries the CAPTURE_EMPTY code.
        raise CaptureEmpty(
            f"dump produced no image (kind={kind}, level={level}) — no {what} "
            f"is currently bound, or the GPU capture failed; try again "
            f"after a frame has been rendered"
        )

    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if kind == "clut":
        file_path = await _save_to_output(data, "cluts", f"clut_{ts}.png")
    else:
        file_path = await _save_to_output(data, "textures", f"tex_level{level}_{ts}.png")

    meta: TextureDumpMeta = {
        "level": level,
        "file_path": file_path,
        "size_bytes": len(data),
        "format": "png",
    }
    return CallToolResult(
        content=[Image(data=data, format="png").to_image_content()],
        structured_content=meta,
    )
