"""GPU stats tool wrapper.

1 tool exposed:
- ppsspp_gpu_stats(session_id) — call `gpu.stats.get` (async ticketed)
  to query GPU statistics (fps, vblanks, info, timing).

READ-ONLY: does not modify memory or CPU state. Requires an active
session. The call is async ticketed: PPSSPP pushes stats on the next
GPU flip, so the game must be running (CPU not stepping) for the call
to return within the timeout.
"""

from __future__ import annotations

import logging
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.errors import ToolError, to_tool_error
from ppsspp_dfx_mcp.models.gpu_stats import GpuStatsResult
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.tools._common import require_session_id, translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.gpu_stats import GpuStatsResponse

GpuStatsOutput = derive_output_contract("GpuStatsOutput", GpuStatsResponse)

logger = logging.getLogger(__name__)

__all__ = ["gpu_stats"]


# Former docstring (kept as comment; description is now the TDQS docstring):
# Query GPU statistics (fps, vblanks, info, timing).
#
# Returns:
# GpuStatsResponse dict: fps / vblanks_per_second / info / timing /
# raw / text.
#
# Raises:
# ToolError: on session lookup failure, empty session_id, or WS
# failure (including timeout when CPU is paused — no frames
# rendered, no stats pushed).
@mcp.tool(
    name="ppsspp_gpu_stats",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
@translate_tool_errors
async def gpu_stats(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
) -> GpuStatsOutput:
    """PURPOSE: Query GPU counters — fps, vblanks per second, timing info.

    USAGE: session_id. The CPU must be RUNNING; paused, the MCP pre-probe returns CPU_STATE_ERROR instead of hanging — which doubles as the cheapest paused-CPU probe.

    BEHAVIOR: READ-ONLY.

    RETURNS: {fps, vblanks_per_second, info, timing, raw, text}."""
    require_session_id(session_id)

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_gpu_stats",
            "session_id": session_id,
        },
    )

    try:
        async with session_client(session_id) as client:
            raw = await client.gpu_stats()
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e

    if not isinstance(raw, dict):
        raw = {}

    result = GpuStatsResult.from_raw(raw)
    return GpuStatsResponse.from_result(result).model_dump(mode="json")
