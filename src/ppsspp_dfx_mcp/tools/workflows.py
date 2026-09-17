"""H1 composite breakpoint-wait tools (2026-09-07).

2 tools exposed:
- ppsspp_wait_breakpoint — block until a breakpoint hit (cpu.stepping
  broadcast) without holding the per-session lock
- ppsspp_trace_memory_access — arm a memory breakpoint, wait for the
  hit, capture pc/registers/backtrace, remove the breakpoint, and
  restore the CPU — all in one call

Lock contract (R16 PARTIAL_HOLD): both tools acquire the per-session
lock ONLY for their short locked sub-operations (arm / probe / capture /
cleanup). The wait itself subscribes to the observer's cpu.stepping
fan-out (see GameStateObserver.subscribe_stepping) and holds NO lock, so
concurrent read/observe calls against the same session keep working —
the gpu_stats-error side channel for hit detection is retired by these
tools. Submitting step/pause/resume during the wait makes the wait
meaningless (the CPU stops running towards the breakpoint); that is the
caller's responsibility, not a lock concern.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.core.game_state_observer import SteppingSubscription
from ppsspp_dfx_mcp.errors import ArgsInvalid, SessionNotFound, ToolError, to_tool_error
from ppsspp_dfx_mcp.models.workflow import (
    FrameSnapshotResult,
    TraceAccessResult,
    WaitBreakpointResult,
)
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session import session_manager
from ppsspp_dfx_mcp.session.client_helper import (
    session_client,
    session_client_with_transport,
    validate_session_alive,
)
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.workflow import (
    FrameSnapshotResponse,
    TraceAccessResponse,
    WaitBreakpointResponse,
)

WaitBreakpointOutput = derive_output_contract("WaitBreakpointOutput", WaitBreakpointResponse)
FrameSnapshotOutput = derive_output_contract(
    "FrameSnapshotOutput",
    FrameSnapshotResponse,
    # 多形态：不可暂停的路径返回 StateObserverResponse（见
    # tools/_common.MULTI_SHAPE_OUTPUT_TOOLS）。SDK 会拿这个契约校验返回值，
    # required 集合会在该分支上硬失败，故全字段可选。
    partial=True,
)
TraceAccessOutput = derive_output_contract("TraceAccessOutput", TraceAccessResponse)

logger = logging.getLogger(__name__)

__all__ = ["wait_breakpoint", "trace_memory_access", "frame_snapshot"]


# Wait budgets are clamped: long enough for a real hit, short enough
# that a stuck call returns control to the agent.
_MIN_TIMEOUT_S = 0.5
_MAX_TIMEOUT_S = 300.0


def _clamp_timeout(timeout_s: float) -> float:
    return min(max(float(timeout_s), _MIN_TIMEOUT_S), _MAX_TIMEOUT_S)


async def _get_live_observer(session_id: str) -> Any:
    """Return the session's GameStateObserver (arming prerequisite).

    Raises a ToolError (not SessionNotFound) when the session has no
    live observer — fake-mode sessions and disk-loaded sessions have
    none, and the message should say that instead of implying the
    session is gone.
    """
    try:
        return await session_manager.get_observer(session_id)
    except SessionNotFound as e:
        raise ArgsInvalid(
            f"session {session_id} has no live broadcast observer — "
            f"wait tools (ppsspp_trace_memory_access / "
            f"ppsspp_wait_breakpoint / ppsspp_wait_frames) require a "
            f"live PPSSPP WebSocket link; fake-mode and disk-loaded "
            f"sessions have none. Recovery: check ppsspp_smoke_test "
            f"(ws_connected), then start a fresh session via "
            f"ppsspp_session(action='start') and retry."
        ) from e


def _find_mem_bp_by_addr(listing: Any, address: int) -> dict[str, Any] | None:
    """Find a memory breakpoint by address in a mem_bp_list response.

    Local copy of the tools/breakpoint.py finder (kept tiny on purpose):
    PPSSPP matches memchecks by address+size, so removal must use the
    memcheck's REAL size — removing with the caller's size alone
    silently fails when it differs.
    """
    bps = listing.get("breakpoints", []) if isinstance(listing, dict) else []
    for bp in bps:
        if isinstance(bp, dict) and int(bp.get("address", 0)) == address:
            return bp
    return None


async def _remove_mem_bp_quietly(session_id: str, address: int) -> bool:
    """Best-effort removal of the memcheck at address (cleanup paths).

    Returns True when the breakpoint table no longer lists the address.
    Never raises — used from exception/timeout cleanup where the
    original error must not be masked.
    """
    try:
        async with session_client(session_id) as client:
            listing = await client.mem_bp_list()
            existing = _find_mem_bp_by_addr(listing, address)
            if existing is None:
                return True
            await client.mem_bp_remove(address=address, size=int(existing.get("size", 4)))
            listing = await client.mem_bp_list()
            return _find_mem_bp_by_addr(listing, address) is None
    except Exception as e:  # noqa: BLE001 — cleanup must not mask callers
        logger.warning(
            "trace cleanup: mem bp remove failed for 0x%08X: %s",
            address,
            e,
        )
        return False


@mcp.tool(
    name="ppsspp_wait_breakpoint",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
    ),
)
@translate_tool_errors
async def wait_breakpoint(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    timeout_s: Annotated[
        float,
        Field(
            default=30.0,
            description=(
                "Wait budget in seconds (default 30, clamped to "
                "[0.5, 300]). On timeout the tool returns hit=false — "
                "NOT an error — so callers can poll."
            ),
        ),
    ] = 30.0,
) -> WaitBreakpointOutput:
    """PURPOSE: Block until a breakpoint hit (any kind) — replaces polling gpu_stats errors as a hit probe.

    USAGE: session_id; timeout_s default 30. Arm a breakpoint first via ppsspp_breakpoint (set or mem_set). Use when you only need to know a hit happened; call ppsspp_trace_memory_access instead to capture the hit scene (registers/backtrace) in one step.

    BEHAVIOR: READ-ONLY. Subscribes to the cpu.stepping broadcast and holds NO session lock — concurrent reads/observes keep working, but do NOT submit step/pause/resume during the wait. An already-paused CPU returns hit=true + already_paused=true with a high-trust pc (a manual pause is indistinguishable from a hit).

    RETURNS: {hit, already_paused, timeout_s, pc, reason, related_address, ticks} — timeout returns hit=false (pollable, not an error); reason/related_address may be null on some builds."""
    budget = _clamp_timeout(timeout_s)
    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_wait_breakpoint", "session_id": session_id},
    )
    await validate_session_alive(session_id)
    observer = await _get_live_observer(session_id)
    subscription: SteppingSubscription = observer.subscribe_stepping()
    try:
        # Arm-time CPU state probe (brief lock): a hit that already
        # happened must be reported instead of waited for forever. The
        # subscription was created BEFORE the probe, so a hit landing
        # between this probe and the wait loop below is buffered, not lost.
        async with session_client_with_transport(session_id) as (
            client,
            transport,
        ):
            status = await transport.call("cpu.status")
            already_paused = bool(status.get("stepping"))
            pc_trusted: int | None = None
            if already_paused:
                pc_trusted, _trust = await client.safe_get_pc()
        if already_paused:
            result = WaitBreakpointResult(
                hit=True,
                already_paused=True,
                pc=pc_trusted,
            )
            return WaitBreakpointResponse.from_result(result, timeout_s=budget).model_dump(
                mode="json"
            )

        # Lock-free wait: consume OUR subscription only.
        deadline = time.monotonic() + budget
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result = WaitBreakpointResult(hit=False)
                return WaitBreakpointResponse.from_result(result, timeout_s=budget).model_dump(
                    mode="json"
                )
            msg = await subscription.get(timeout_s=remaining)
            if msg is not None:
                result = WaitBreakpointResult(
                    hit=True,
                    already_paused=False,
                    pc=msg.get("pc"),
                    reason=msg.get("reason"),
                    related_address=msg.get("relatedAddress"),
                    ticks=msg.get("ticks"),
                )
                return WaitBreakpointResponse.from_result(result, timeout_s=budget).model_dump(
                    mode="json"
                )
    finally:
        subscription.close()


# ── ppsspp_frame_snapshot (P4, H2) ───────────────────────────────────────


@mcp.tool(
    name="ppsspp_frame_snapshot",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    ),
)
@translate_tool_errors
async def frame_snapshot(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    probes: Annotated[
        str,
        Field(
            default="",
            description=(
                "Optional comma-separated state_observer registry probe "
                "names to capture alongside the CPU state (empty = none)."
            ),
        ),
    ] = "",
    want_registers: Annotated[
        bool,
        Field(
            default=True,
            description="Include the full CPU register dump (GPR/FPU/VFPU).",
        ),
    ] = True,
) -> FrameSnapshotOutput:
    """PURPOSE: One-call paused scene snapshot — pause (unless already paused), capture pc + registers + optional named probes, then resume.

    USAGE: session_id; probes = optional comma-separated state_observer registry names; want_registers default true. Prefer this over a manual pause + query(registers) + resume sequence.

    BEHAVIOR: STATE-CHANGE. The session lock is held for the whole call (pause→capture→resume is short). A CPU we paused is resumed before returning; an already-paused CPU stays paused. A failing capture never leaves the game frozen.

    RETURNS: {was_stepping, resumed, pc, trust_level, registers, probes} — registers/probes keys are ALWAYS present; they carry null when opted out (want_registers=false / probes omitted) — F-8 nullable-key contract, 2026-09-08."""
    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_frame_snapshot", "session_id": session_id},
    )
    try:
        async with session_client_with_transport(session_id) as (
            client,
            transport,
        ):
            status = await transport.call("cpu.status")
            was_stepping = bool(status.get("stepping"))
            if not was_stepping:
                await client.pause()
            try:
                pc, trust = await client.safe_get_pc()
                result: dict[str, Any] = {
                    "was_stepping": was_stepping,
                    "resumed": False,
                    "pc": pc,
                    "trust_level": trust,
                }
                if want_registers:
                    result["registers"] = await client.get_all_regs()
                if probes:
                    # Import locally to break the circular dependency (same
                    # pattern as batch_step's state_probe step).
                    from ppsspp_dfx_mcp.tools.state_observer import (
                        _observe_probes,
                        _resolve_target_probes,
                        _seed_from_yaml,
                    )
                    from ppsspp_dfx_mcp.views.state_observer import (
                        StateObserverResponse,
                    )

                    _seed_from_yaml()
                    observation = await _observe_probes(client, _resolve_target_probes(probes), 1)
                    result["probes"] = StateObserverResponse.from_observe(observation).model_dump(
                        mode="json"
                    )
            except Exception:
                # Never leave the game frozen when OUR pause started it.
                if not was_stepping:
                    try:
                        await client.resume()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("frame_snapshot: recovery resume failed: %s", e)
                raise
            if not was_stepping:
                await client.resume()
                result["resumed"] = True
            return FrameSnapshotResponse.from_result(FrameSnapshotResult(**result)).model_dump(
                mode="json"
            )
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e


@mcp.tool(
    name="ppsspp_trace_memory_access",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    ),
)
@translate_tool_errors
async def trace_memory_access(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    address: Annotated[
        str,
        Field(
            description=("Address to trace, as a hex string (e.g. '0x08A0D000')."),
        ),
    ],
    access: Annotated[
        Literal["read", "write", "read_write"],
        Field(
            default="read",
            description=("Access kind to trap: 'read', 'write', or 'read_write' (default 'read')."),
        ),
    ] = "read",
    size: Annotated[
        int,
        Field(
            default=4,
            description="Watch size in bytes: 1, 2, or 4 (default 4).",
        ),
    ] = 4,
    timeout_s: Annotated[
        float,
        Field(
            default=30.0,
            description=(
                "Wait budget in seconds (default 30, clamped to "
                "[0.5, 300]). On timeout: hit=false, breakpoint removed."
            ),
        ),
    ] = 30.0,
    want_registers: Annotated[
        bool,
        Field(
            default=False,
            description="Include the full CPU register dump in the hit.",
        ),
    ] = False,
    want_backtrace: Annotated[
        bool,
        Field(
            default=False,
            description="Include the HLE backtrace in the hit (CPU is "
            "paused at the hit, so the trace is valid).",
        ),
    ] = False,
) -> TraceAccessOutput:
    """PURPOSE: One-call answer to 'what code reads/writes this address' — arm a memory breakpoint, wait for the hit, capture pc (+registers/backtrace), remove the breakpoint, and resume.

    USAGE: session_id + hex address; access='read'|'write'|'read_write' (default read); size 1/2/4 (default 4); timeout_s default 30; want_registers/want_backtrace optional. Game must be RUNNING (call after session wait_ready).

    BEHAVIOR: MUTATING. Arms a temporary breakpoint and always removes it (list-verified real-size removal). The lock is held only for arm/capture/cleanup — the wait is lock-free (concurrent reads OK). Do NOT run other breakpoint/step tools during the wait: the first cpu.stepping broadcast wins. An already-paused CPU short-circuits (nothing can hit). Error paths still remove the breakpoint and resume.

    RETURNS: {hit, already_paused, address, access, timeout_s, hits:[{pc, related_address, reason, ticks, mem_hits?, registers?, backtrace?}], bp_removed, resumed, note}. reason/related_address may be null on some builds; mem_hits is the attribution counter."""
    budget = _clamp_timeout(timeout_s)
    addr = parse_address(address)
    if addr == 0:
        raise ArgsInvalid(f"address must be a valid hex address, got {address!r}")
    if size not in (1, 2, 4):
        raise ArgsInvalid(f"size must be 1, 2, or 4 bytes, got {size}")
    read_flag = access in ("read", "read_write")
    write_flag = access in ("write", "read_write")

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_trace_memory_access",
            "session_id": session_id,
            "address": address,
            "access": access,
        },
    )
    await validate_session_alive(session_id)
    observer = await _get_live_observer(session_id)
    subscription: SteppingSubscription = observer.subscribe_stepping()
    bp_armed = False
    needs_resume = False
    try:
        # ── Locked region 1: probe state + arm the breakpoint ──
        async with session_client_with_transport(session_id) as (
            client,
            transport,
        ):
            status = await transport.call("cpu.status")
            was_stepping = bool(status.get("stepping"))
            if was_stepping:
                # Nothing can hit while the CPU is paused; leaving the
                # short-circuit explicit (instead of waiting out the
                # budget for a broadcast that can never come) keeps the
                # timeout semantics honest.
                result = TraceAccessResult(
                    hit=False,
                    already_paused=True,
                    bp_removed=False,
                    resumed=False,
                    note="CPU already paused at arm time — resume it first, then trace",
                )
                return TraceAccessResponse.from_result(
                    result, address=addr, access=access, timeout_s=budget
                ).model_dump(mode="json")
            await client.mem_bp_add(
                address=addr,
                size=size,
                read=read_flag,
                write=write_flag,
                enabled=True,
                log=False,
            )
            bp_armed = True

        # ── Lock-free wait ──
        deadline = time.monotonic() + budget
        broadcast: dict[str, Any] | None = None
        while broadcast is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            broadcast = await subscription.get(timeout_s=remaining)

        if broadcast is None:
            # Timeout: remove our breakpoint, leave CPU state untouched
            # (it never paused — no hit happened).
            bp_removed = await _remove_mem_bp_quietly(session_id, addr)
            if bp_removed:
                bp_armed = False
            result = TraceAccessResult(
                hit=False,
                bp_removed=bp_removed,
                resumed=False,
                note=None
                if bp_removed
                else "breakpoint removal failed on the timeout path — check "
                "ppsspp_breakpoint(action='mem_list')",
            )
            return TraceAccessResponse.from_result(
                result, address=addr, access=access, timeout_s=budget
            ).model_dump(mode="json")

        # From here the CPU is paused by OUR breakpoint: if anything
        # fails before the capture region resumes it, the except
        # handler must restore the game instead of leaving it frozen.
        needs_resume = True

        # ── Locked region 2: capture the scene, clean up, restore ──
        # The hit paused the CPU; PC in the broadcast is the trustworthy
        # post-hit PC.
        async with session_client(session_id) as client:
            entry: dict[str, Any] = {
                "pc": broadcast.get("pc"),
                "related_address": broadcast.get("relatedAddress"),
                "reason": broadcast.get("reason"),
                "ticks": broadcast.get("ticks"),
            }
            if want_registers:
                entry["registers"] = await client.get_all_regs()
            if want_backtrace:
                entry["backtrace"] = await client.backtrace()
            # Cleanup BEFORE resume so the (still armed) breakpoint
            # cannot re-hit between remove and resume.
            listing = await client.mem_bp_list()
            existing = _find_mem_bp_by_addr(listing, addr)
            if existing is not None:
                # S3 (2026-09-07): capture the hit counter BEFORE removal —
                # independent attribution evidence that OUR breakpoint fired
                # (the broadcast's reason/relatedAddress are absent on some
                # PPSSPP builds; the counter only increments for this addr).
                entry["mem_hits"] = int(existing.get("hits", 0))
                await client.mem_bp_remove(address=addr, size=int(existing.get("size", size)))
            listing = await client.mem_bp_list()
            bp_removed = _find_mem_bp_by_addr(listing, addr) is None
            if bp_removed:
                bp_armed = False
            await client.resume()
            needs_resume = False
        result = TraceAccessResult(
            hit=True,
            hits=[entry],
            bp_removed=bp_removed,
            resumed=True,
            note=None
            if bp_removed
            else "hit captured but breakpoint removal could not be verified — "
            "check ppsspp_breakpoint(action='mem_list')",
        )
        return TraceAccessResponse.from_result(
            result, address=addr, access=access, timeout_s=budget
        ).model_dump(mode="json")
    except Exception:
        # The hit paused the CPU and the exception fired before the
        # capture region could resume it — do not leave the game frozen
        # with our breakpoint logic half-done.
        if needs_resume:
            try:
                async with session_client(session_id) as client:
                    await client.resume()
            except Exception as e:  # noqa: BLE001 — recovery is best-effort
                logger.warning("trace cleanup: resume after hit failed: %s", e)
        raise
    finally:
        if bp_armed:
            await _remove_mem_bp_quietly(session_id, addr)
        subscription.close()
