"""Context tool — one-call crash-triage pack for an address.

Identity (nearest known function from addresses.yaml, IDA-space converted
via top_base, within a 4 KiB attribution cap) + disassembly window +
optional backtrace (paused CPU only).
Pure client-side orchestration; no new WS events. Replaces the manual
3-call sequence documented in the crash_analysis playbook.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import format_address, format_address_fields, parse_address
from ppsspp_dfx_mcp.config import addresses as _addresses
from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.session.client_helper import resolve_session_id, session_client
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views.context import (
    ContextResponse,
    DisasmLineView,
    IdentityView,
)

logger = logging.getLogger(__name__)

_DEFAULT_WINDOW = 8
_MAX_WINDOW = 32

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    ContextOutput = dict[str, Any]
else:
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
    top_base = data.get("top_base")
    top = top_base if isinstance(top_base, dict) else {}
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


# G-13 / FR-013: attribution cap. known_functions carries function starts
# only (no sizes), so an address is attributed to the nearest start at or
# below it -- but only while it stays within this many bytes of that start.
# 0x1000 (4 KiB) is a plausible upper bound on a TOPX function body
# (plan.md FR-013). Without the cap, 0x09FFF000 resolved to sub_12CAB8 at
# offset 23913800: every address above the lowest known start got
# "explained" as an offset into some function, which misleads crash triage.
_MAX_IDENTITY_DISTANCE = 0x1000


def _match_identity(address: int, funcs: dict[str, int]) -> tuple[IdentityView | None, str]:
    """Nearest function at/below the address, within the attribution cap.

    Returns ``(view, "")`` when the nearest start is within
    ``_MAX_IDENTITY_DISTANCE`` bytes of the address; ``(None, reason)``
    naming that nearest symbol when it is farther than the cap; and
    ``(None, "")`` when no function starts at/below the address at all
    (a knowledge gap, not a near miss).
    """
    candidates = [(start, name) for name, start in funcs.items() if start <= address]
    if not candidates:
        return None, ""
    start, name = max(candidates)
    distance = address - start
    if distance > _MAX_IDENTITY_DISTANCE:
        return None, (
            f"nearest known function {name} starts at {format_address(start)},"
            f" {distance} bytes below the address - beyond the"
            f" 0x{_MAX_IDENTITY_DISTANCE:X}-byte attribution cap, so the"
            f" address is not in a known function range"
        )
    return IdentityView(name=name, start=format_address(start), offset=distance), ""


def _parse_range_bound(raw: Any) -> int:
    """Parse a memory-mapping bound in any observed shape.

    The wire shape (PPSSPP ``memory.mapping``) carries decimal ints
    (views/memory_map.py); hex strings appear in knowledge fixtures. A
    bare ``int(str(x), 16)`` turned the decimal wire value 134217728 into
    0x134217728 — larger than any PSP address — so ``region`` silently
    resolved to "" on every real session (review-v4 W-2).
    """
    if isinstance(raw, int):
        return raw
    s = str(raw or "").strip()
    if not s:
        return 0
    return int(s, 16) if s.lower().startswith("0x") else int(s, 10)


def _match_region(address: int, ranges: list[dict[str, Any]]) -> str:
    for r in ranges or []:
        try:
            lo = _parse_range_bound(r.get("start") or r.get("address") or 0)
            hi_raw = r.get("end")
            size_raw = r.get("size")
            if hi_raw is None and size_raw is not None:
                hi_raw = _parse_range_bound(size_raw) + lo
            hi = 0 if hi_raw is None else _parse_range_bound(hi_raw)
            if lo <= address < hi:
                return str(r.get("type") or r.get("name") or "")
        except (TypeError, ValueError):
            continue
    return ""


@mcp.tool(
    name="ppsspp_context",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
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
    SCOPE: identity/region come from a TOPX-specific address knowledge base (addresses.yaml). For any other ISO they come back empty and `note` says so explicitly -- an empty identity means 'not in the tables', NOT 'bad address'.

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
    identity, identity_miss = _match_identity(addr, _known_functions_runtime())
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

    disasm_views: list[DisasmLineView] = []
    for line in raw_lines or []:
        addr_value = line.get("address")
        if addr_value is None:
            addr_text = ""
        elif isinstance(addr_value, str):
            addr_text = addr_value
        else:
            addr_text = format_address(int(addr_value))
        disasm_views.append(DisasmLineView(address=addr_text, text=str(line.get("text", ""))))
    # Normalize address fields in backtrace frames (PPSSPP returns decimal
    # ints for pc/entry; format_address_fields converts them to hex strings
    # so the caller never sees mixed formats).
    normalized_backtrace = format_address_fields(backtrace_list)
    # An empty identity/region means the address knowledge base does
    # not cover this binary -- it is NOT evidence that the address is bad.
    # Say which case this is, or the caller cannot tell them apart.
    if identity is None:
        if identity_miss:
            # Near miss: the nearest symbol exists but is beyond the
            # attribution cap (G-13). Keep it in the note for orientation.
            note = f"{note}; identity unresolved: {identity_miss}"
        else:
            note = (
                f"{note}; identity unresolved: the address tables are TOPX-specific,"
                f" so for a non-TOPX image this field is not applicable"
                f" (unknown/not applicable, not a bad address)"
            )
    if not region:
        note = (
            f"{note}; region empty: address 0x{addr:08X} is outside every"
            f" reported memory range (region unknown/not applicable)"
        )

    return ContextResponse.build(
        addr, identity, region, disasm_views, normalized_backtrace, note
    ).model_dump(mode="json")
