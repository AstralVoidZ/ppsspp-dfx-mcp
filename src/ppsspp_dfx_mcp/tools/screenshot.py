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
from typing import Annotated, Any, Literal, TypedDict

from mcp.server.mcpserver import Image
from mcp.types import CallToolResult, ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError, to_tool_error
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import resolve_session_id, session_capture
from ppsspp_dfx_mcp.tools._common import save_output_bytes, translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
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


async def _save_to_output(data: bytes, subdir: str, filename: str) -> str:
    """Save data to .ppsspp-dfx/output/{subdir}/{filename}. Returns file path.

    P2 refactor: delegates to the shared containment + to_thread helper
    (W12) — multi-MB PNG writes no longer stall the event loop.
    """
    return await save_output_bytes(subdir, filename, data)


# Sentinel for "mode not explicitly provided by caller".
_MODE_DEFAULT = "auto"


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
        assert source in ("render", "output")
        return (source, source)
    if mode_explicit:
        logger.warning(
            "ppsspp_screenshot: `mode` parameter is deprecated; use `source` "
            "(Literal['render', 'output']) instead. Got mode=%r.",
            mode,
        )
    assert mode in ("auto", "wm_command", "printwindow", "vram")
    return (mode, mode)


# Former docstring (kept as comment; description is now the TDQS docstring):
# Capture a PPSSPP framebuffer screenshot.
#
# Returns [TextContent(metadata_json), ImageContent(image)] for token
# efficiency. On failure, returns [TextContent(metadata_json)] only.
@mcp.tool(
    name="ppsspp_screenshot",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
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
    # Track whether the USER explicitly passed
    # `mode`. The default fill below must not count as explicit — the
    # previous flag made every no-arg screenshot call log the deprecation
    # warning for a parameter the caller never sent.
    session_id = await resolve_session_id(session_id)
    user_set_mode = mode is not None
    if not user_set_mode and source is None:
        mode = _MODE_DEFAULT

    strategy, label = _resolve_capture_strategy(source, mode, user_set_mode)

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
    try:
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
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e

    img_format = _detect_format(data)
    w, h = _image_dims(data) if data else (0, 0)

    file_path = ""
    if data:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        ext = "jpg" if img_format == "jpeg" else "png"
        file_path = await _save_to_output(data, "screenshots", f"{ts}_{label}.{ext}")

    meta = {
        "mode": label,
        "source": source,
        "file_path": file_path,
        "size_bytes": len(data),
        "width": w,
        "height": h,
        "format": img_format,
        # An empty capture previously surfaced only
        # implicitly (size_bytes=0, no ImageContent). Make it explicit so
        # agents can branch on failure without parsing heuristics — same
        # intent as dump_texture's CAPTURE_EMPTY error.
        "empty": not data,
    }

    # ImageContent carries the pixels; structuredContent carries the metadata.
    # An empty capture yields metadata only (empty=true), not an error.
    # NOTE: `Image` is a b64 helper, not a content model — it must be converted
    # via `to_image_content()` before entering a hand-built CallToolResult
    # (the SDK only does that conversion on the `convert_result` path, which
    # building the result ourselves bypasses).
    content: list[Any] = []
    if data:
        content.append(Image(data=data, format=img_format).to_image_content())
    return CallToolResult(content=content, structured_content=meta)


# Former docstring (kept as comment; description is now the TDQS docstring):
# Dump the currently-bound GPU texture as PNG.
#
# Returns [TextContent(metadata_json), ImageContent(image)] for token
# efficiency. On failure, returns [TextContent(metadata_json)] only.
# Former docstrings (kept as comment; description is now the TDQS docstring):
# ppsspp_dump_texture / ppsspp_dump_clut: dump the currently-bound GPU
# texture / CLUT palette as PNG. Merged into ppsspp_dump(kind=...) in
# v0.1.6 (Glama surface review: tool-count reduction).
@mcp.tool(
    name="ppsspp_dump",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
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
    try:
        async with session_capture(session_id) as (_client, capture):
            if kind == "clut":
                data = await capture.dump_clut()
            else:
                data = await capture.dump_texture(level=level)
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e
    # An empty capture means PPSSPP could not deliver the payload (nothing
    # bound at this state) — surface it as an error instead of a success
    # with size_bytes=0.
    if not data:
        what = "CLUT" if kind == "clut" else "texture"
        raise ToolError(
            f"dump produced no image (kind={kind}, level={level}) — no {what} "
            f"is currently bound, or the GPU capture failed; try again "
            f"after a frame has been rendered",
            code="CAPTURE_EMPTY",
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
