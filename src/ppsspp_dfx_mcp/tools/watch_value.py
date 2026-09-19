"""ppsspp_watch_value — value-change polling watch (D2 方案B-3).

热读地址的观察点替代：纯读轮询，零暂停成本。值变化才记录
（旧值/新值/帧序号/相对时间）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Annotated, Any, Literal, TypedDict

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import (
    session_client,
    validate_session_alive,
)
from ppsspp_dfx_mcp.tools._common import translate_tool_errors

logger = logging.getLogger(__name__)

__all__ = ["watch_value"]

_WIDTHS: dict[str, int] = {"u8": 1, "u16": 2, "u32": 4}


class WatchValueOutput(TypedDict):
    """Structured output contract for ppsspp_watch_value."""

    address: str
    size: int
    mode: str
    samples: int
    first_value: int | None
    last_value: int | None
    changes: list[dict[str, Any]]
    change_count: int


@mcp.tool(
    name="ppsspp_watch_value",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@translate_tool_errors
async def watch_value(
    session_id: Annotated[str, Field(description="Active session ID.")],
    address: Annotated[
        str,
        Field(description="Address to watch, hex string (e.g. '0x08A0D000')."),
    ],
    mode: Annotated[
        Literal["u8", "u16", "u32"],
        Field(default="u32", description="Value interpretation for change detection."),
    ] = "u32",
    interval_frames: Annotated[
        int,
        Field(default=60, description="Poll cadence in frames (60 = 1s wall clock)."),
    ] = 60,
    duration_frames: Annotated[
        int,
        Field(
            default=600,
            description="Total watch window in frames (cap 18000 ~ 5 min at 60fps).",
        ),
    ] = 600,
) -> WatchValueOutput:
    """PURPOSE: Value-change watch on an address — zero-pause alternative to a read watchpoint.

    USAGE: session_id + address + mode + interval. Polls the value every
    interval_frames and records changes (old/new/frame/time). Pure reads:
    the CPU is never paused, so hot addresses are safe (D2 storm-free).

    BEHAVIOR: READ-ONLY. Polling loop; never mutates state; blocks ~duration_frames/60 seconds
    (cap 18000 frames).

    RETURNS: {address, size, mode, samples, first_value, last_value,
    changes: [{frame, t_s, old, new}], change_count}.
    """
    addr_int = parse_address(address)
    size = _WIDTHS[mode]
    if duration_frames > 18000:
        raise ArgsInvalid(f"duration_frames {duration_frames} exceeds the cap 18000")
    if interval_frames < 1:
        raise ArgsInvalid(f"interval_frames must be >= 1 (got {interval_frames})")

    await validate_session_alive(session_id)
    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_watch_value", "address": addr_int},
    )

    changes: list[dict[str, Any]] = []
    first_value: int | None = None
    last_value: int | None = None
    samples = 0
    frame = 0
    t0 = time.monotonic()

    async with session_client(session_id) as client:
        while frame < duration_frames:
            raw = await client.read_bytes(address=addr_int, size=size)
            value = int.from_bytes(raw[:size], "little", signed=False)
            if first_value is None:
                first_value = value
            elif value != last_value:
                changes.append(
                    {
                        "frame": frame,
                        "t_s": round(time.monotonic() - t0, 3),
                        "old": last_value,
                        "new": value,
                    }
                )
                if len(changes) >= 64:
                    break
            last_value = value
            samples += 1
            for _ in range(interval_frames):
                await asyncio.sleep(1 / 60)
                frame += 1

    return {
        "address": f"0x{addr_int:08X}",
        "size": size,
        "mode": mode,
        "samples": samples,
        "first_value": first_value,
        "last_value": last_value,
        "changes": changes,
        "change_count": len(changes),
    }
