"""Context tool — one-call crash-triage pack for an address.

Identity (nearest known function from addresses.yaml, IDA-space converted
via top_base) + disassembly window + optional backtrace (paused CPU only).
Pure client-side orchestration; no new WS events. Replaces the manual
3-call sequence documented in the crash_analysis playbook.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.config import addresses as _addresses
from ppsspp_dfx_mcp.errors import ArgsInvalid, to_tool_error
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import resolve_session_id, session_client
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.context import (
    ContextResponse,
    DisasmLineView,
    IdentityView,
)

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW = 8
_MAX_WINDOW = 32

ContextOutput = derive_output_contract("ContextOutput", ContextResponse)


def _known_functions_runtime() -> dict[str, int]:
    """known_functions converted from IDA space to runtime addresses.

    YAML schema (addresses.yaml):
        known_functions:
          <name>: <ida_address int>   # comments carry the human description

    Conversion: runtime = ida + (top_base.ppsspp - top_base.ida), falling
    back to the project default 0x08804000 when top_base is absent.
    """
    data = _addresses()
    funcs = data.get("known_functions") if isinstance(data, dict) else None
    if not isinstance(funcs, dict):
        return {}
    top = data.get("top_base") if isinstance(data.get("top_base"), dict) else {}
    ida = top.get("ida", 0x00000000)
    ppsspp = top.get("ppsspp", 0x08804000)
    ida = ida if isinstance(ida, int) else 0x00000000
    ppsspp = ppsspp if isinstance(ppsspp, int) else 0x08804000
    offset = ppsspp - ida
    out: dict[str, int] = {}
    for name, value in funcs.items():
        if isinstance(value, int) and not isinstance(value, bool):
            out[str(name)] = value + offset
    return out


def _match_identity(address: int, funcs: dict[str, int]) -> IdentityView | None:
    """Nearest function whose start is at/below the address."""
    candidates = [(start, name) for name, start in funcs.items() if start <= address]
    if not candidates:
        return None
    start, name = max(candidates)
    return IdentityView(name=name, start=f"0x{start:08X}", offset=address - start)


def _match_region(address: int, ranges: list[dict[str, Any]]) -> str:
    for r in ranges or []:
        try:
            lo = int(str(r.get("start") or r.get("address") or 0), 16)
            hi_raw = r.get("end")
            if hi_raw is None and r.get("size") is not None:
                hi_raw = int(r.get("size")) + lo
            hi = int(str(hi_raw or 0), 16) if isinstance(hi_raw, str) else int(hi_raw or 0)
            if lo <= address < hi:
                return str(r.get("type") or r.get("name") or "")
        except (TypeError, ValueError):
            continue
    return ""


@mcp.tool(
    name="ppsspp_context",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
@translate_tool_errors
async def context(
    address: Annotated[
        str,
        Field(
            description=(
                "Address to triage, hex ('0x088EF0F4') or decimal — typically "
                "a crash PC or a call target."
            ),
        ),
    ],
    session_id: Annotated[
        str | None,
        Field(
            description=("Active session ID; auto-resolved when exactly one session is active."),
        ),
    ] = None,
    window: Annotated[
        int,
        Field(
            description=(
                "Disassembly instructions BEFORE the address (same count "
                "after, so 2*window+1 total; default 8, cap 32)."
            ),
        ),
    ] = _DEFAULT_WINDOW,
    include_backtrace: Annotated[
        bool,
        Field(
            description=(
                "Include the call stack (default false). Requires a paused "
                "CPU — the tool pauses/resumes around it; concurrent readers "
                "block briefly."
            ),
        ),
    ] = False,
) -> ContextOutput:
    """PURPOSE: One-call crash-triage pack — identity + disassembly window + optional backtrace for an address.

    USAGE: pass a crash PC or call target; identity resolves via addresses.yaml known_functions (IDA offset applied); disasm covers `window` instructions before/after; include_backtrace=true adds the call stack (pauses the CPU briefly).

    BEHAVIOR: READ-ONLY. Unknown addresses return identity=null and the raw window instead of failing; backtrace is skipped (not an error) when the CPU is running — the note field says why.

    ROUTING: persistent breakpoints around this address -> ppsspp_breakpoint; one armed hit-capture -> ppsspp_breakpoint(action="trace"); recurring sampling -> ppsspp_state_observer.

    RETURNS: {address, identity: {name, start, offset} | null, region, disasm: [{address, text}], backtrace: [...], backtrace_note}."""
    addr = parse_address(address)
    if addr <= 0:
        raise ArgsInvalid(f"address must be > 0, got {address!r}")
    if not 0 < window <= _MAX_WINDOW:
        raise ArgsInvalid(f"window must be in [1, {_MAX_WINDOW}], got {window}")
    session_id = await resolve_session_id(session_id)
    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_context", "address": hex(addr), "window": window},
    )
    try:
        identity = _match_identity(addr, _known_functions_runtime())
        async with session_client(session_id) as client:
            ranges = (await client.memory_map()).get("ranges") or []
            region = _match_region(addr, ranges)
            begin = max(1, addr - window * 4)
            raw_lines = await client.disasm(address=begin, count=window * 2 + 1)
            backtrace_list: list[dict[str, Any]] = []
            note = ""
            if include_backtrace:
                async with client.with_stepping():
                    bt = await client.backtrace()
                backtrace_list = list(bt.get("frames") or bt.get("backtrace") or [])
                if not backtrace_list:
                    note = "backtrace empty at pause point"
            else:
                note = "backtrace skipped (include_backtrace=false)"
    except Exception as e:
        raise to_tool_error(e) from e

    disasm_views: list[DisasmLineView] = []
    for line in raw_lines or []:
        addr_value = line.get("address")
        addr_text = (
            (addr_value if isinstance(addr_value, str) else f"0x{int(addr_value):08X}")
            if addr_value is not None
            else ""
        )
        disasm_views.append(DisasmLineView(address=addr_text, text=str(line.get("text", ""))))
    return ContextResponse.build(
        addr, identity, region, disasm_views, backtrace_list, note
    ).model_dump(mode="json")
