"""Memory-map tool wrapper.

1 tool exposed:
- ppsspp_memory_map(session_id) — call `memory.mapping` (no params)
  to list memory regions (ram / vram / sram, primary / mirror).

READ-ONLY: does not modify memory or CPU state.
"""

from __future__ import annotations

import logging
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.errors import ToolError, to_tool_error
from ppsspp_dfx_mcp.models.memory_map import MemoryMapResult
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.tools._common import require_session_id, translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.memory_map import MemoryMapResponse

MemoryMapOutput = derive_output_contract("MemoryMapOutput", MemoryMapResponse)

logger = logging.getLogger(__name__)

__all__ = ["memory_map"]


# Former docstring (kept as comment; description is now the TDQS docstring):
# Get the PPSSPP memory region map.
#
# Returns:
# MemoryMapResponse dict: ranges + mapping + text.
#
# Raises:
# ToolError: on session lookup failure, empty session_id, or WS
# failure.
@mcp.tool(
    name="ppsspp_memory_map",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
@translate_tool_errors
async def memory_map(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
) -> MemoryMapOutput:
    """PURPOSE: Get the PPSSPP memory region map (user / kernel / VRAM ranges).

    USAGE: session_id.

    BEHAVIOR: READ-ONLY.

    RETURNS: {ranges[], mapping, text}."""
    require_session_id(session_id)

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_memory_map",
            "session_id": session_id,
        },
    )

    try:
        async with session_client(session_id) as client:
            response = await client.memory_map()
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e

    mapping = response if isinstance(response, dict) else {}
    if not isinstance(mapping, dict):
        mapping = {}

    result = MemoryMapResult(mapping=mapping)
    return MemoryMapResponse.from_result(result).model_dump(mode="json")
