"""GPU record dump tool wrapper.

1 tool exposed:
- ppsspp_gpu_record(session_id) — call `gpu.record.dump` (async ticketed)
  to capture the next frame's GE command stream as a binary dump.

READ-ONLY: does not modify memory or CPU state. Requires an active
session. The call is async ticketed: PPSSPP records the next frame's
GE commands and returns them, so the game must be running (CPU not
stepping) for the call to return within the timeout.

Auto-save: the binary dump is saved to
`.ppsspp-dfx/output/gpu_dumps/<timestamp>.dump` with a timestamped
filename. The JSON response includes `file_path` and `size_bytes`; the
binary data itself is NOT embedded in the JSON (it can be large).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.errors import ToolError, to_tool_error
from ppsspp_dfx_mcp.models.gpu_record import GpuRecordResult
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.tools._common import (
    require_session_id,
    save_output_bytes,
    translate_tool_errors,
)
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.gpu_record import GpuRecordResponse

GpuRecordOutput = derive_output_contract("GpuRecordOutput", GpuRecordResponse)

logger = logging.getLogger(__name__)

__all__ = ["gpu_record"]


async def _save_dump(data: bytes) -> str:
    """Save dump to .ppsspp-dfx/output/gpu_dumps/<timestamp>.dump.

    Returns the absolute file path as a string.
    Uses asyncio.to_thread to avoid blocking the event loop.
    """
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    return await save_output_bytes("gpu_dumps", f"{ts}.dump", data)


# Former docstring (kept as comment; description is now the TDQS docstring):
# Capture a GPU record dump (GE command stream for one frame).
#
# Returns:
# GpuRecordResponse dict: size_bytes / file_path / raw / text.
#
# Raises:
# ToolError: on session lookup failure, empty session_id, or WS
# failure (including timeout when CPU is paused — no frames
# rendered, no dump returned).
@mcp.tool(
    name="ppsspp_gpu_record",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
@translate_tool_errors
async def gpu_record(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
) -> GpuRecordOutput:
    """PURPOSE: Capture the next rendered frame's GE command stream as a binary dump file.

    USAGE: session_id. The CPU must be RUNNING — a paused GPU never flips a frame; the MCP pre-probe converts that into a clean CPU_STATE_ERROR.

    BEHAVIOR: READ-ONLY. Captures to a binary file under output/gpu_dumps/ (not JSON).

    RETURNS: {size_bytes, file_path, raw, text}."""
    require_session_id(session_id)

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_gpu_record",
            "session_id": session_id,
        },
    )

    try:
        async with session_client(session_id) as client:
            raw = await client.gpu_record_dump()
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e

    if not isinstance(raw, dict):
        raw = {}

    result = GpuRecordResult.from_raw(raw)

    file_path = ""
    if result.size > 0:
        try:
            file_path = await _save_dump(result.data)
        except OSError as e:
            logger.warning(
                "ppsspp_gpu_record: failed to save dump (%s); returning size without file_path",
                e,
                exc_info=True,
            )

    return GpuRecordResponse.from_result(result, file_path).model_dump(mode="json")
