"""Memory info search tool wrapper.

1 tool exposed:
- ppsspp_memory_info_search(session_id, match, address?, end?, type?) —
  call `memory.info.search` to query PPSSPP's memory tracking system
  for allocation/write/texture metadata tags.

READ-ONLY: does not modify memory or CPU state. Requires an active
session. The call is synchronous RPC: PPSSPP returns the matching
regions in the same response.
"""

from __future__ import annotations

import logging
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError, to_tool_error
from ppsspp_dfx_mcp.models.memory_info_search import MemoryInfoSearchResult
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.tools._common import require_session_id, translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.memory_info_search import MemoryInfoSearchResponse

MemoryInfoSearchOutput = derive_output_contract("MemoryInfoSearchOutput", MemoryInfoSearchResponse)

logger = logging.getLogger(__name__)

__all__ = ["memory_info_search"]


# Former docstring (kept as comment; description is now the TDQS docstring):
# Search memory allocation/write/texture metadata tags.
#
# Returns:
# MemoryInfoSearchResponse dict: regions / count / raw / text.
#
# Raises:
# ToolError: on session lookup failure, empty session_id, or WS
# failure.
@mcp.tool(
    name="ppsspp_memory_info_search",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
@translate_tool_errors
async def memory_info_search(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    match: Annotated[
        str,
        Field(
            description=(
                "Case-insensitive substring to match against memory "
                "region tags (e.g., 'texture', 'vertex', 'framebuf')."
            ),
        ),
    ],
    address: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional start address for the search range, as a hex "
                "string (e.g. '0x08804000'). If "
                "omitted, PPSSPP searches the entire address space."
            ),
        ),
    ] = None,
    end: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional end address for the search range, as a hex "
                "string (e.g. '0x08810000'). If "
                "omitted, PPSSPP searches to the end of the address space."
            ),
        ),
    ] = None,
    type: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional type filter (e.g., 'texture', 'vertex'). If "
                "omitted, all region types are returned."
            ),
        ),
    ] = None,
) -> MemoryInfoSearchOutput:
    """PURPOSE: Search PPSSPP's memory-tracking metadata for allocation/texture tags matching a string.

    USAGE: session_id + match (case-insensitive substring, required); optional address/end/type filters.

    BEHAVIOR: READ-ONLY. Returns a single extent per matching tag.

    RETURNS: {regions[], count, raw, text}."""
    require_session_id(session_id)
    # An empty match would match everything —
    # require a non-empty substring.
    if not match or not match.strip():
        raise ArgsInvalid("match must be a non-empty substring")
    address_int = parse_address(address) if address is not None else None
    end_int = parse_address(end) if end is not None else None

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_memory_info_search",
            "session_id": session_id,
            "match": match,
            "address": address_int,
            "end": end_int,
            "type": type,
        },
    )

    try:
        async with session_client(session_id) as client:
            raw = await client.memory_info_search(
                match=match, address=address_int, end=end_int, type=type
            )
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e

    if not isinstance(raw, dict):
        raw = {}

    result = MemoryInfoSearchResult.from_raw(raw)
    return MemoryInfoSearchResponse.from_result(result).model_dump(mode="json")
