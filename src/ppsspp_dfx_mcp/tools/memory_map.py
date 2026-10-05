"""Memory-map tool wrapper.

1 tool exposed:
- ppsspp_memory_map(session_id) — call `memory.mapping` (no params)
  to list memory regions (ram / vram / sram, primary / mirror).

READ-ONLY: does not modify memory or CPU state.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.models.memory_map import MemoryMapResult
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import require_session_id, translate_tool_errors
from ppsspp_dfx_mcp.views.memory_map import MemoryMapResponse

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    MemoryMapOutput = dict[str, Any]
else:
    MemoryMapOutput = derive_output_contract("MemoryMapOutput", MemoryMapResponse)

logger = logging.getLogger(__name__)

__all__ = ["memory_map"]


@mcp.tool(
    name="ppsspp_memory_map",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
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

    async with session_client(session_id) as client:
        response = await client.memory_map()

    mapping = response if isinstance(response, dict) else {}

    result = MemoryMapResult(mapping=mapping)
    return MemoryMapResponse.from_result(result).model_dump(mode="json")
