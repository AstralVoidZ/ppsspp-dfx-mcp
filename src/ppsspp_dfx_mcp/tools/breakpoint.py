"""Breakpoint tool wrapper.

1 tool exposed:
- ppsspp_breakpoint(action, ...) — aggregate CPU + memory breakpoint
  operations (set / remove / list / update, mem_set / mem_remove /
  mem_list / mem_update)

Async: uses session_client → PpssppDebugClient (composes WsTransport
+ SteppingManager) under the hood. Tools call DebugClient domain
methods (cpu_bp_* / mem_bp_*) directly; no orchestration wrapper
indirection.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import format_address, parse_address
from ppsspp_dfx_mcp.errors import ArgsInvalid, BreakpointError, ToolError, to_tool_error
from ppsspp_dfx_mcp.models.breakpoint import BreakpointResult
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import (
    resolve_session_id,
    session_client,
    validate_session_alive,
)
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.breakpoint import BreakpointResponse

BreakpointOutput = derive_output_contract(
    "BreakpointOutput",
    BreakpointResponse,
    partial=True,  # 多形态：wait/trace 返回命中形状，管理动作返回断点表形状
)

logger = logging.getLogger(__name__)

__all__ = ["breakpoint"]

_BP_ACTIONS: tuple[str, ...] = (
    "wait",
    "trace",
    "stats",
    "set",
    "remove",
    "list",
    "update",
    "mem_set",
    "mem_remove",
    "mem_list",
    "mem_update",
)
_BP_ACTIONS_REQUIRING_ADDRESS: frozenset[str] = frozenset(
    {"trace", "set", "remove", "update", "mem_set", "mem_remove", "mem_update"}
)


def _find_mem_bp(
    list_resp: dict[str, Any], address: int, size: int | None = None
) -> dict[str, Any] | None:
    """Find a memory breakpoint by address in a mem_bp_list response.

    PPSSPP's memory.breakpoint.list returns ``{"breakpoints": [...]}``
    where each entry has ``address``, ``size``, ``read``, ``write``,
    ``change`` fields. Used by mem_update to fetch the current state
    for merging partial read/write/change updates, and by mem_remove to
    resolve the *actual* size of the memcheck being deleted.

    When ``size`` is given the match requires an exact size; otherwise
    the first breakpoint at ``address`` wins (PPSSPP matches memchecks
    by start+end pair, so an address hosts at most one).
    """
    bps = list_resp.get("breakpoints", []) if isinstance(list_resp, dict) else []
    for bp in bps:
        if not isinstance(bp, dict):
            continue
        if int(bp.get("address", 0)) != address:
            continue
        if size is None or int(bp.get("size", 0)) == size:
            return bp
    return None


async def _probe_snapshot(session_id: str) -> dict[str, int]:
    """Read every registered state probe once (name -> unsigned value).

    Registry emptiness (addresses.yaml without `state_probes`) is
    tolerated — stats is best-effort sampling, not a read contract.
    Individual probe read failures are swallowed (value dropped).
    """
    from ppsspp_dfx_mcp.tools.state_observer import (
        _observe_probes,
        _resolve_target_probes,
        _seed_from_yaml,
    )

    _seed_from_yaml()
    try:
        probes = _resolve_target_probes("")
    except ArgsInvalid:
        return {}
    async with session_client(session_id) as client:
        result = await _observe_probes(client, probes, 1)
    return {o.name: o.value for o in result.observations if not o.error}


async def _probe_changes(session_id: str, baseline: dict[str, int]) -> list[dict[str, Any]]:
    """Diff registered state probes against a window-start baseline."""
    current = await _probe_snapshot(session_id)
    return [
        {
            "probe": name,
            "old": baseline.get(name),
            "new": current.get(name),
            "ts": round(time.time(), 1),
        }
        for name in current
        if baseline.get(name) != current.get(name)
    ]


@mcp.tool(
    name="ppsspp_breakpoint",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    ),
)
@translate_tool_errors
async def breakpoint(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    action: Annotated[
        Literal[
            "wait",
            "trace",
            "stats",
            "set",
            "remove",
            "list",
            "update",
            "mem_set",
            "mem_remove",
            "mem_list",
            "mem_update",
        ],
        Field(
            description=(
                "Breakpoint operation. Valid values:\n"
                "Consumption actions (lifecycle orchestration):\n"
                "- 'wait': STRICT-WAIT — block until any breakpoint is hit \n"
                "(arm nothing; set/mem_set first). Lock-free: concurrent \n"
                "reads keep working. The breakpoint stays armed.\n"
                "- 'stats': HIT-FREQUENCY — count breakpoint hits by pc \n"
                "over a time window; optionally samples probe value \n"
                "changes via state_observer. Read-only.\n"
                "- 'trace': HIT-SNAPSHOT-RESUME — arm a temporary \n"
                "MEMORY breakpoint at `address`, wait for the hit, \n"
                "capture pc/registers/backtrace, ALWAYS remove it, then \n"
                "resume (defaults to read access; narrow with \n"
                "read/write/size). For EXECUTION breakpoints use \n"
                "action='set' + 'wait' instead.\n"
                "CPU breakpoint actions:\n"
                "- 'set': add a CPU execution breakpoint (requires address; "
                "enabled? defaults to True; condition? optional).\n"
                "- 'remove': delete a CPU breakpoint by address.\n"
                "- 'list': list all current CPU breakpoints.\n"
                "- 'update': update a CPU breakpoint's enabled/log/condition/"
                "log_format (requires address; all other params optional).\n"
                "Memory breakpoint actions:\n"
                "- 'mem_set': add a memory access breakpoint (requires "
                "address; size?/read?/write?/enabled?/log?/condition?/"
                "log_format?).\n"
                "- 'mem_remove': delete a memory breakpoint by address. "
                "Delete semantics are STRICT: removing a non-existent "
                "memcheck is an ERROR (unlike ppsspp_state_observer "
                "action=clear, which is idempotent-ok — F-5 contract, "
                "2026-09-08).\n"
                "- 'mem_list': list all current memory breakpoints.\n"
                "- 'mem_update': update a memory breakpoint's enabled/log/"
                "condition/log_format (requires address)."
            ),
        ),
    ],
    address: Annotated[
        str,
        Field(
            default="0x0",
            description=(
                "Breakpoint address, as a hex string (e.g. '0x08804000'). "
                "Required for set / remove / update / "
                "mem_set / mem_remove / mem_update; ignored for list / "
                "mem_list."
            ),
        ),
    ] = "0x0",
    enabled: Annotated[
        bool | None,
        Field(
            default=None,
            description=(
                "Breakpoint enable flag. For action='set' / 'mem_set', "
                "defaults to True when None. For action='update' / "
                "'mem_update', None means 'don't change'. Ignored for "
                "remove / list actions."
            ),
        ),
    ] = None,
    log: Annotated[
        bool | None,
        Field(
            default=None,
            description=(
                "Log flag (update / mem_set / mem_update only; None = "
                "don't change). For mem_set, defaults to False when None."
            ),
        ),
    ] = None,
    condition: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Break condition expression (set / update / mem_set / "
                "mem_update; None = don't send)."
            ),
        ),
    ] = None,
    log_format: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Log format string (update / mem_set / mem_update only; None = don't change)."
            ),
        ),
    ] = None,
    size: Annotated[
        int,
        Field(
            default=4,
            description=(
                "Memory breakpoint watch size in bytes (mem_set / mem_remove / "
                "mem_update; default 4). Fixed-width watches use 1/2/4; "
                "larger sizes are passed through to PPSSPP as a range "
                "watch. PPSSPP matches memory breakpoints by address+size "
                "pair, so remove/update must pass the exact size recorded "
                "at set time."
            ),
        ),
    ] = 4,
    read: Annotated[
        bool | None,
        Field(
            default=None,
            description=(
                "Trigger on read access (mem_set only; None defaults to "
                "True). For mem_update, passing read triggers a merge "
                "query — omit to leave read unchanged."
            ),
        ),
    ] = None,
    write: Annotated[
        bool | None,
        Field(
            default=None,
            description=(
                "Trigger on write access (mem_set only; None defaults to "
                "True). For mem_update, passing write triggers a merge "
                "query — omit to leave write unchanged."
            ),
        ),
    ] = None,
    timeout_s: Annotated[
        float,
        Field(
            default=30.0,
            description=(
                "Wait budget in seconds (wait / trace only; default 30, "
                "clamped to [0.5, 300]). On timeout: hit=false — NOT an "
                "error — so callers can poll."
            ),
        ),
    ] = 30.0,
    want_registers: Annotated[
        bool,
        Field(
            default=False,
            description=("Include the full CPU register dump in the hit (trace only)."),
        ),
    ] = False,
    want_backtrace: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Include the HLE backtrace in the hit (trace only; CPU is "
                "paused at the hit, so the trace is valid)."
            ),
        ),
    ] = False,
) -> BreakpointOutput:
    """PURPOSE: Manage breakpoints AND consume their hits — set/remove/update/list CPU execution breakpoints and memory watchpoints, strict-wait for a hit, or one-call arm-hit-capture-resume tracing.

    USAGE: management actions as below; action='wait' blocks until any breakpoint is hit (set/mem_set first; lock-free; breakpoint stays armed); action='trace' arms a temporary MEMORY breakpoint at `address`, waits, captures pc/registers/backtrace, always removes it and resumes (defaults to read access; narrow with read/write/size). For EXECUTION breakpoints use action='set' + 'wait'.


    ROUTING: persistent breakpoint management -> here; one-shot strict-wait -> action='wait'; armed hit-capture -> action='trace'.
    BEHAVIOR: MUTATING. trace arms/removes and set/mem_* manage state; Reliable hits need CPUCore=2 (IR Interpreter). mem_remove resolves the watchpoint's real size via mem_list first (address+size matching); mem_update merges existing read/write/change unconditionally (PPSSPP zero-omits omitted bools). CPU set/remove return no data — the tool follows with a list for verification. wait/trace are lock-free during the wait itself (concurrent reads keep working); do NOT submit step/pause/resume during a wait.

    RETURNS: stats → {mode:"stats", window_s, total_hits, by_pc: [{pc, count, first_seen, last_seen}], probe_changes?: [{probe, old, new, ts}], note}; management actions → {action, address, enabled, breakpoints[]}; wait → {hit, already_paused, timeout_s, pc, reason, related_address, ticks}; trace → {hit, already_paused, address, access, timeout_s, hits: [{pc, related_address, reason, ticks, mem_hits?, registers?, backtrace?}], bp_removed, resumed, note}."""
    if action == "wait":
        from ppsspp_dfx_mcp.tools.workflows import wait_breakpoint

        return await wait_breakpoint(session_id=session_id, timeout_s=timeout_s)
    if action == "trace":
        from ppsspp_dfx_mcp.tools.workflows import trace_memory_access

        access_v = "read_write" if (read and write) else ("write" if write else "read")
        return await trace_memory_access(
            session_id=session_id,
            address=address,
            access=access_v,
            size=size,
            timeout_s=timeout_s,
            want_registers=want_registers,
            want_backtrace=want_backtrace,
        )
    if action == "stats":
        from ppsspp_dfx_mcp.tools.workflows import _get_live_observer

        budget = min(max(timeout_s, 0.5), 300.0)
        session_id = await resolve_session_id(session_id)
        await validate_session_alive(session_id)
        probe_baseline = await _probe_snapshot(session_id)
        observer = await _get_live_observer(session_id)
        sub = observer.subscribe_stepping()
        try:
            deadline = time.monotonic() + budget
            by_pc: dict[int, dict[str, Any]] = {}
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                msg = await sub.get(timeout_s=remaining)
                if msg is None:
                    continue
                pc = msg.get("pc")
                if pc is None:
                    continue
                e = by_pc.setdefault(
                    pc,
                    {
                        "pc": f"0x{pc:08X}",
                        "count": 0,
                        "first_seen": round(time.time(), 1),
                        "last_seen": round(time.time(), 1),
                    },
                )
                e["count"] += 1
                e["last_seen"] = round(time.time(), 1)
            probe_changes = await _probe_changes(session_id, probe_baseline)
            return {
                "mode": "stats",
                "window_s": round(budget, 1),
                "total_hits": sum(e["count"] for e in by_pc.values()),
                "by_pc": sorted(by_pc.values(), key=lambda x: -x["count"]),
                "probe_changes": probe_changes,
                "note": "",
            }
        finally:
            sub.close()
    if action not in _BP_ACTIONS:
        raise ArgsInvalid(f"invalid action={action!r}; expected one of {_BP_ACTIONS}")
    address_int = parse_address(address)
    if action in _BP_ACTIONS_REQUIRING_ADDRESS and address_int == 0:
        # Not worded "address is required": this also fires when the caller
        # explicitly passes 0x0, so the message says what actually happened.
        raise ArgsInvalid(
            f"address 0x0 is not a valid breakpoint target (the zero "
            f"address is reserved) for action={action!r}"
        )
    if action in ("mem_set", "mem_remove", "mem_update") and size < 1:
        # A zero/negative-width watchpoint is stored by PPSSPP but can
        # never hit, and it stacks invisibly with same-address memchecks.
        raise ArgsInvalid(
            f"invalid memcheck size {size} for action={action!r} — must "
            f"be a positive byte count (1/2/4 typical; removal matches "
            f"the watchpoint's exact address+size pair)"
        )

    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_breakpoint", "action": action, "session_id": session_id},
    )

    # For 'set' / 'mem_set', None → True (DebugClient default).
    effective_enabled = enabled if enabled is not None else True

    try:
        async with session_client(session_id) as client:
            if action == "set":
                await client.cpu_bp_add(
                    address=address_int,
                    enabled=effective_enabled,
                    condition=condition,
                )
                # cpu.breakpoint.add is fire-and-forget (no return data);
                # follow-up breakpoint.list to populate the result.
                resp = await client.cpu_bp_list()
                bps = resp.get("breakpoints", []) if isinstance(resp, dict) else []
                result = BreakpointResult(
                    action=action,
                    address=address_int,
                    enabled=effective_enabled,
                    breakpoints=bps,
                )
            elif action == "remove":
                # Align with mem_remove semantics: removing a breakpoint
                # that doesn't exist is an error, not a silent no-op
                # (PPSSPP's remove succeeds silently even when nothing
                # matched, so check the pre-listing).
                pre = await client.cpu_bp_list()
                pre_bps = pre.get("breakpoints", []) if isinstance(pre, dict) else []
                if not any(
                    int(bp.get("address", 0)) == address_int
                    for bp in pre_bps
                    if isinstance(bp, dict)
                ):
                    raise BreakpointError(f"no CPU breakpoint at 0x{address_int:08X}")
                await client.cpu_bp_remove(address=address_int)
                resp = await client.cpu_bp_list()
                bps = resp.get("breakpoints", []) if isinstance(resp, dict) else []
                result = BreakpointResult(
                    action=action, address=address_int, enabled=True, breakpoints=bps
                )
            elif action == "list":
                resp = await client.cpu_bp_list()
                bps = resp.get("breakpoints", []) if isinstance(resp, dict) else []
                result = BreakpointResult(action=action, address=0, enabled=True, breakpoints=bps)
            elif action == "update":
                await client.cpu_bp_update(
                    address=address_int,
                    enabled=enabled,
                    log=log,
                    condition=condition,
                    log_format=log_format,
                )
                # cpu.breakpoint.update returns no business data; follow-up list.
                resp = await client.cpu_bp_list()
                bps = resp.get("breakpoints", []) if isinstance(resp, dict) else []
                result = BreakpointResult(
                    action=action,
                    address=address_int,
                    enabled=effective_enabled,
                    breakpoints=bps,
                )
            elif action == "mem_set":
                # read/write default None → True for mem_set (backward compat).
                effective_read = read if read is not None else True
                effective_write = write if write is not None else True
                await client.mem_bp_add(
                    address=address_int,
                    size=size,
                    read=effective_read,
                    write=effective_write,
                    enabled=effective_enabled,
                    log=log if log is not None else False,
                    condition=condition,
                    log_format=log_format,
                )
                # memory.breakpoint.add returns no business data; follow-up list.
                resp = await client.mem_bp_list()
                bps = resp.get("breakpoints", []) if isinstance(resp, dict) else []
                result = BreakpointResult(
                    action=action,
                    address=address_int,
                    enabled=effective_enabled,
                    breakpoints=bps,
                )
            elif action == "mem_remove":
                # PPSSPP matches memchecks by exact start+end pair
                # (BreakpointSubscriber.cpp memory.breakpoint.remove uses
                # address+size). Resolve the *actual* size of the memcheck
                # at this address first — removing with the caller's size
                # alone silently fails when it differs (e.g. a 16-byte
                # watch removed with the default size=4).
                listing = await client.mem_bp_list()
                existing = _find_mem_bp(listing if isinstance(listing, dict) else {}, address_int)
                if existing is None:
                    # Fail loudly whenever no memcheck exists at this
                    # address, regardless of the caller's size: deferring
                    # to a non-default caller size sends a remove that
                    # cannot match anything and surfaces as silent success.
                    raise BreakpointError(
                        f"no memory breakpoint at {format_address(address_int)}; "
                        "check ppsspp_breakpoint(action=mem_list)",
                        code="BREAKPOINT_ERROR",
                    )
                actual_size = int(existing.get("size", size))
                await client.mem_bp_remove(address=address_int, size=actual_size)
                resp = await client.mem_bp_list()
                bps = resp.get("breakpoints", []) if isinstance(resp, dict) else []
                result = BreakpointResult(
                    action=action, address=address_int, enabled=True, breakpoints=bps
                )
            elif action == "mem_list":
                resp = await client.mem_bp_list()
                bps = resp.get("breakpoints", []) if isinstance(resp, dict) else []
                result = BreakpointResult(action=action, address=0, enabled=True, breakpoints=bps)
            else:  # mem_update
                # PPSSPP's WebSocketMemoryBreakpointUpdate takes OPTIONAL
                # bool params that default to false (NOT keep-current —
                # BreakpointSubscriber.cpp:L285), so the current
                # read/write/change must be fetched and merged back
                # UNCONDITIONALLY, matching the existing memcheck by
                # ADDRESS ONLY (mem_remove's actual-size pattern) and
                # forwarding the memcheck's real size. Matching by the
                # caller's size can miss a memcheck whose actual size
                # differs from the default (e.g. a 16-byte watch vs
                # size=4) and leave the merge empty, silently resetting
                # read/write to false.
                listing = await client.mem_bp_list()
                existing = _find_mem_bp(listing if isinstance(listing, dict) else {}, address_int)
                cur_read = existing.get("read", True) if existing else True
                cur_write = existing.get("write", True) if existing else True
                cur_change = existing.get("change", False) if existing else False
                actual_size = int(existing.get("size", size)) if existing else size
                merged_read = read if read is not None else cur_read
                merged_write = write if write is not None else cur_write
                merged_change = cur_change

                await client.mem_bp_update(
                    address=address_int,
                    size=actual_size,
                    enabled=enabled,
                    log=log,
                    condition=condition,
                    log_format=log_format,
                    read=merged_read,
                    write=merged_write,
                    change=merged_change,
                )
                # memory.breakpoint.update returns no business data; follow-up list.
                resp = await client.mem_bp_list()
                bps = resp.get("breakpoints", []) if isinstance(resp, dict) else []
                result = BreakpointResult(
                    action=action,
                    address=address_int,
                    enabled=effective_enabled,
                    breakpoints=bps,
                )
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e
    return BreakpointResponse.from_result(result).model_dump(mode="json")
