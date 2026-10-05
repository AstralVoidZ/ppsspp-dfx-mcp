"""Memory info search tool wrapper.

1 tool exposed:
- ppsspp_search_memory_info(session_id, match, address?, end?, type?) —
  call `memory.info.search` to query PPSSPP's memory tracking system
  for allocation/write/texture metadata tags.

READ-ONLY: does not modify memory or CPU state. Requires an active
session. The call is synchronous RPC: PPSSPP returns the matching
regions in the same response.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.models.search_memory_info import SearchMemoryInfoResult
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import require_session_id, translate_tool_errors
from ppsspp_dfx_mcp.views.search_memory_info import SearchMemoryInfoResponse

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    SearchMemoryInfoOutput = dict[str, Any]
else:
    SearchMemoryInfoOutput = derive_output_contract(
        "SearchMemoryInfoOutput", SearchMemoryInfoResponse
    )

logger = logging.getLogger(__name__)

__all__ = ["search_memory_info"]


@mcp.tool(
    name="ppsspp_search_memory_info",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
@translate_tool_errors
async def search_memory_info(
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
) -> SearchMemoryInfoOutput:
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
            "tool": "ppsspp_search_memory_info",
            "session_id": session_id,
            "match": match,
            "address": address_int,
            "end": end_int,
            "type": type,
        },
    )

    async with session_client(session_id) as client:
        raw = await client.search_memory_info(
            match=match, address=address_int, end=end_int, type=type
        )

    if not isinstance(raw, dict):
        raw = {}

    result = SearchMemoryInfoResult.from_raw(raw)
    return SearchMemoryInfoResponse.from_result(result).model_dump(mode="json")
