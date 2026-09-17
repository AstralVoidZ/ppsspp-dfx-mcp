"""Search-disassembly tool wrapper.

1 tool exposed:
- ppsspp_search_disasm(session_id, address, match, end?, max_results?)
  search disassembly for instructions whose text contains `match`
  (case-insensitive) via `memory.searchDisasm`.

READ-ONLY: does not modify memory or CPU state.

PPSSPP API contract (Core/Debugger/WebSocket/DisasmSubscriber.cpp):
- `address` (u32, required): start address.
- `end` (u32, optional): end address; if <= address, a loop search
  is performed (default behavior).
- `match` (string, required): case-insensitive substring to find in
  the disassembly text.
- `displaySymbols` (bool, optional, default true): whether to render
  symbol names in the output.

PPSSPP protocol limitations (batch 2 protocol-adapter layer):
- `match` does not support `$` register prefix (PPSSPP's
  MIPSDebugInterface omits `$` from register names; see
  MIPSDebugInterface.cpp:281-290). The tool strips `$` from match
  before forwarding so callers can use idiomatic MIPS syntax like
  "jr $ra".
- `memory.searchDisasm` returns only {"address": <u32>|null} — no
  instruction text. The tool calls `client.disasm()` on each matched
  address to populate the `text` field in each result entry.

Output governance:
- PPSSPP's `memory.searchDisasm` returns only the FIRST match per call.
  The tool loops: after finding a match at address M, it searches
  again from M+4 until no more matches, max_results reached, or a
  loop is detected (seen address repeats). This collects up to
  `max_results` matches (default 100) instead of just one.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError, to_tool_error
from ppsspp_dfx_mcp.models.search_disasm import SearchDisasmResult
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.tools._common import require_session_id, translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.search_disasm import SearchDisasmResponse

SearchDisasmOutput = derive_output_contract("SearchDisasmOutput", SearchDisasmResponse)

logger = logging.getLogger(__name__)

__all__ = ["search_disasm"]

# Default cap for loop search results. Prevents unbounded
# WS round-trips when the match pattern is extremely common (e.g.,
# matching "nop" would find thousands of hits in code sections).
_DEFAULT_MAX_RESULTS = 100


# Former docstring (kept as comment; description is now the TDQS docstring):
# Search disassembly for instructions matching a substring.
#
# PPSSPP's ``memory.searchDisasm`` returns only the first
# match per call. This tool loops: after finding a match at address M,
# it searches again from M+4, collecting up to ``max_results`` matches.
#
# Returns:
# SearchDisasmResponse dict: address + match + end + results +
# text.
#
# Raises:
# ToolError: on session lookup failure, empty session_id,
# address <= 0, empty match, or WS failure.
@mcp.tool(
    name="ppsspp_search_disasm",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
@translate_tool_errors
async def search_disasm(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    address: Annotated[
        str,
        Field(
            description=(
                "Starting address for the disassembly search, as a hex "
                "string (e.g. '0x08804000')."
            ),
        ),
    ],
    match: Annotated[
        str,
        Field(
            description=(
                "Case-insensitive substring to search for in the "
                "disassembly text (e.g., 'jal', 'addiu', 'lw r5'). "
                "May include '$' register prefix (e.g. 'jr $ra') — "
                "automatically stripped before forwarding to PPSSPP "
                "(PPSSPP register names have no '$' prefix). "
                "Required by PPSSPP's memory.searchDisasm event."
            ),
        ),
    ],
    end: Annotated[
        str,
        Field(
            default="0x0",
            description=(
                "End address for the search range, as a hex string "
                "(e.g. '0x08810000'). If 0 or equal to `address`, "
                "PPSSPP performs a loop search (wraps around memory)."
            ),
        ),
    ] = "0x0",
    max_results: Annotated[
        int,
        Field(
            default=_DEFAULT_MAX_RESULTS,
            description=(
                "Maximum number of matches to collect (default 100). "
                "PPSSPP returns only the first match per call; the tool "
                "loops from each match address+4 until no more matches, "
                "this cap is reached, or a loop is detected."
            ),
        ),
    ] = _DEFAULT_MAX_RESULTS,
) -> SearchDisasmOutput:
    """PURPOSE: Loop-search disassembly for a substring, collecting matching instructions with context.
    
    USAGE: session_id + match (a leading '$' is stripped); start address; end=0 wraps the search around the whole region; max_results default 100.
    
    BEHAVIOR: READ-ONLY.
    
    RETURNS: {address, match, end, results[{address, text, name, params}], text}."""
    require_session_id(session_id)
    address_int = parse_address(address)
    if address_int <= 0:
        raise ArgsInvalid("address must be > 0")
    end_int = parse_address(end)
    if not match:
        raise ArgsInvalid("match is required (non-empty string)")
    if max_results <= 0:
        raise ArgsInvalid("max_results must be > 0")

    # Strip the '$' register prefix — PPSSPP's MIPSDebugInterface omits
    # '$' from register names (MIPSDebugInterface.cpp:281-290). Callers
    # may use idiomatic MIPS syntax like "jr $ra"; stripping '$' makes
    # it match PPSSPP's "jr ra" output. Safe because '$' only appears
    # as register prefix in MIPS disassembly text.
    sanitized_match = match.replace("$", "")

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_search_disasm",
            "session_id": session_id,
            "address": address_int,
            "match": match,
            "end": end_int,
            "max_results": max_results,
        },
    )

    try:
        async with session_client(session_id) as client:
            results = await _collect_matches(
                client=client,
                start_addr=address_int,
                match=sanitized_match,
                end=end_int,
                max_results=max_results,
            )
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e

    result = SearchDisasmResult(
        address=address_int, match=match, end=end_int, results=results
    )
    return SearchDisasmResponse.from_result(result).model_dump(mode="json")


async def _collect_matches(
    client: Any,
    start_addr: int,
    match: str,
    end: int,
    max_results: int,
) -> list[dict[str, Any]]:
    """Loop search to collect multiple matches.

    PPSSPP's ``memory.searchDisasm`` returns only the first match per
    call. This function loops: after finding a match at address M, it
    searches again from M+4. Termination conditions:

    1. PPSSPP returns ``{"address": null}`` → no more matches.
    2. ``len(results) >= max_results`` → cap reached.
    3. Matched address already in ``seen`` set → loop complete
       (PPSSPP's loop search wraps around and re-finds earlier matches).

    For range search (``end > start_addr``), also stops when
    ``current >= end`` (search range exhausted).
    """
    results: list[dict[str, Any]] = []
    seen: set[int] = set()
    current = start_addr

    while len(results) < max_results:
        # Range search: stop when current exceeds end.
        if end > start_addr and current >= end:
            break

        response = await client.search_disasm(
            address=current, match=match, end=end
        )
        matched_addr = (
            response.get("address") if isinstance(response, dict) else None
        )
        if not isinstance(matched_addr, int):
            break  # No more matches.

        if matched_addr in seen:
            break  # Loop complete — PPSSPP wrapped around.

        seen.add(matched_addr)

        # PPSSPP memory.searchDisasm returns only
        # {"address": <u32>|null} — no instruction text. Call
        # client.disasm() on the matched address to populate the
        # `text` field so callers see what instruction matched.
        entry: dict[str, Any] = {"address": matched_addr}
        try:
            lines = await client.disasm(address=matched_addr, count=1)
            if lines:
                entry["text"] = lines[0].get("text", "")
                if "name" in lines[0]:
                    entry["name"] = lines[0]["name"]
                if "params" in lines[0]:
                    entry["params"] = lines[0]["params"]
        except Exception:
            # Disasm failure should not mask the match itself —
            # leave entry with address only.
            pass

        results.append(entry)
        current = matched_addr + 4

    return results
