"""Composite breakpoint-wait implementations (2026-09-07).

The tool surface moved to ppsspp_breakpoint(action="wait"/"trace")
(see tools/breakpoint.py); the functions here remain as the delegated
implementations and are no longer registered as MCP tools:
- wait_breakpoint — block until a breakpoint hit (cpu.stepping
  broadcast) without holding the per-session lock
- trace_memory_access — arm a memory breakpoint, wait for the
  hit, capture pc/registers/backtrace, remove the breakpoint, and
  restore the CPU — all in one call

Lock contract: both tools acquire the per-session
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

import contextlib
import logging
import time
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import format_address, parse_address
from ppsspp_dfx_mcp.core import cond_filter
from ppsspp_dfx_mcp.core.game_state_observer import SteppingSubscription
from ppsspp_dfx_mcp.core.value_expr import extract_value
from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.models.workflow import (
    FrameSnapshotResult,
    TraceAccessResult,
    WaitBreakpointResult,
)
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.service.observer_lookup import get_live_observer
from ppsspp_dfx_mcp.session.client_helper import (
    session_client,
    session_client_with_transport,
    validate_session_alive,
)
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.tools._memcheck import find_mem_bp
from ppsspp_dfx_mcp.views.workflow import (
    FrameSnapshotResponse,
    TraceAccessResponse,
    WaitBreakpointResponse,
)

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    WaitBreakpointOutput = dict[str, Any]
    FrameSnapshotOutput = dict[str, Any]
    TraceAccessOutput = dict[str, Any]
else:
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

__all__ = ["frame_snapshot"]  # wait/trace now internal (breakpoint delegates)


# Wait budgets are clamped: long enough for a real hit, short enough
# that a stuck call returns control to the agent.
_MIN_TIMEOUT_S = 0.5
_MAX_TIMEOUT_S = 300.0


def _clamp_timeout(timeout_s: float) -> float:
    return min(max(float(timeout_s), _MIN_TIMEOUT_S), _MAX_TIMEOUT_S)


# 命中风暴熔断阈值：同一地址 ≥10 次命中且相邻间隔 <1s 视为风暴。
_STORM_HITS = 10
_STORM_GAP_S = 1.0


def _filter_address_for(session_id: str, msg: dict[str, Any]) -> int | None:
    """Resolve which armed cond_filter entry a cpu.stepping hit belongs to.

    CPU execution breakpoints report the breakpoint address in ``pc``; memory
    watchpoints report the watched address in ``relatedAddress``. Try both so
    a hit is attributed by the address the caller actually registered.
    """
    for key in ("relatedAddress", "pc"):
        raw = msg.get(key)
        if raw is None:
            continue
        try:
            addr = int(raw)
        except (TypeError, ValueError):
            continue
        if cond_filter.get(session_id, addr) is not None:
            return addr
    return None


async def _evaluate_condition(session_id: str, expression: str) -> bool | None:
    """Evaluate a breakpoint condition MCP-side. Returns None on failure.

    PPSSPP IR mode ignores register conditions (see core/cond_filter.py), so
    the truth value has to be computed here. Failure (WS error / unparseable
    result) is reported as None — the caller treats it conservatively as a
    HIT, because a debugging session must never silently drop a real hit just
    because the evaluator was momentarily unavailable.
    """
    try:
        async with session_client(session_id) as client:
            resp = await client.evaluate(expression=expression)
    except Exception as e:  # noqa: BLE001 — evaluator failure is non-fatal
        logger.warning("cond_filter: evaluate(%r) failed: %s", expression, e)
        return None
    value = extract_value(resp if isinstance(resp, dict) else None)
    if value is None:
        logger.warning("cond_filter: evaluate(%r) returned no numeric value: %r", expression, resp)
        return None
    return value != 0


async def _storm_break(
    *,
    session_id: str,
    address: int,
    entry: dict[str, Any],
    msg: dict[str, Any],
    timeout_s: float,
    filtered_total: int,
) -> dict[str, Any]:
    """Disarm an address that keeps producing fast (falsy) hits.

    ≥10 hits within <1s gaps means the condition never holds on a hot
    address. Remove the breakpoint, drop its filter, and resume the CPU —
    every storm hit was falsy (a truthy one would have returned already), so
    leaving the CPU paused would only freeze the game behind a breakpoint the
    registry no longer tracks.
    """
    note = (
        f"hit storm at {format_address(address)}: {_STORM_HITS}+ hits with "
        f"<{_STORM_GAP_S:g}s gaps — breakpoint removed and filter dropped "
        f"(condition={entry['condition']!r})"
    )
    try:
        async with session_client(session_id) as client:
            # 条件过滤器不记录断点类型：CPU 与内存断点都尝试撤除，避免
            # “撤了 CPU 断点却留下同地址 memcheck”继续冻结 CPU。
            with contextlib.suppress(Exception):
                await client.cpu_bp_remove(address=address)
            with contextlib.suppress(Exception):
                listing = await client.mem_bp_list()
                existing = _find_mem_bp_by_addr(listing, address)
                if existing is not None:
                    await client.mem_bp_remove(address=address, size=int(existing.get("size", 4)))
            with contextlib.suppress(Exception):
                await client.resume()
    except Exception as e:  # noqa: BLE001 — 撤防尽力而为，不得吞掉 storm 报告
        logger.warning("cond_filter: storm-breaker removal failed: %s", e)
    cond_filter.drop(session_id, address)
    result = WaitBreakpointResult(
        hit=False,
        already_paused=False,
        pc=msg.get("pc"),
        reason=msg.get("reason"),
        related_address=msg.get("relatedAddress"),
        ticks=msg.get("ticks"),
        condition=entry["condition"],
        condition_filtered=entry["filtered"],
        filtered_hits=filtered_total,
        storm_break=True,
    )
    out = WaitBreakpointResponse.from_result(result, timeout_s=timeout_s).model_dump(mode="json")
    out["note"] = note
    return out


def _find_mem_bp_by_addr(listing: Any, address: int) -> dict[str, Any] | None:
    """Find a memory breakpoint by address in a mem_bp_list response.

    Delegates to the shared implementation (review-v4 A-17 — this was a
    hand-maintained copy; PPSSPP matches memchecks by address+size, so
    removal must use the memcheck's REAL size).
    """
    return find_mem_bp(listing, address)


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

    USAGE: session_id; timeout_s default 30. Arm a breakpoint first via ppsspp_breakpoint (set or mem_set). Use when you only need to know a hit happened; call ppsspp_breakpoint(action='trace') instead to capture the hit scene (registers/backtrace) in one step.


    ROUTING: one-shot block-until-hit -> here; persistent breakpoint add/remove -> ppsspp_breakpoint; read/write access watch -> ppsspp_breakpoint(action='trace').
    BEHAVIOR: READ-ONLY. Subscribes to the cpu.stepping broadcast and holds NO session lock — concurrent reads/observes keep working, but do NOT submit step/pause/resume during the wait. An already-paused CPU returns hit=true + already_paused=true with a high-trust pc (a manual pause is indistinguishable from a hit).

    RETURNS: {hit, already_paused, timeout_s, pc, reason, related_address, ticks} — timeout returns hit=false (pollable, not an error); reason/related_address may be null on some builds."""
    budget = _clamp_timeout(timeout_s)
    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_breakpoint", "action": "wait", "session_id": session_id},
    )
    await validate_session_alive(session_id)
    observer = await get_live_observer(session_id)
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
            # 已在暂停态：无法归因这次暂停是否来自本地址的（条件）断点——
            # 手动暂停与断点命中在该构建上不可区分，故保守返回 hit=True。
            # v3 审查（真机取证）补：若该 PC 注册过条件过滤器，必须让 Agent
            # 知道条件**未被求值**，否则会把"已暂停的假命中"读成"条件已成立"。
            out = WaitBreakpointResponse.from_result(
                WaitBreakpointResult(
                    hit=True,
                    already_paused=True,
                    pc=pc_trusted,
                ),
                timeout_s=budget,
            ).model_dump(mode="json")
            armed = cond_filter.get(session_id, pc_trusted) if pc_trusted is not None else None
            if armed is not None:
                # armed 非空 ⇒ pc_trusted 非空（上面三元仅在 pc_trusted 非空时
                # 取值）；此断言仅向类型检查器传达该不变式。
                assert pc_trusted is not None
                out["note"] = (
                    f"CPU was already paused at arm time; the condition "
                    f"{armed['condition']!r} registered for "
                    f"{format_address(pc_trusted)} was NOT evaluated (a manual "
                    f"pause is indistinguishable from a hit). Resume the CPU to "
                    f"let the MCP-side evaluator take over."
                )
            return out

        # Lock-free wait: consume OUR subscription only.
        #
        # PPSSPP IR 模式忽略寄存器条件，条件由 MCP 侧求值。带条件的
        # 地址命中后不再立即返回，而是 evaluate 表达式：假 → 自动 resume 继续
        # 等待（不跳出原 wait 语义，总预算仍受 timeout_s 约束）；真 → 正常返回。
        deadline = time.monotonic() + budget
        filtered_total = 0
        hit_stamps: dict[int, list[float]] = {}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result = WaitBreakpointResult(hit=False, filtered_hits=filtered_total)
                return WaitBreakpointResponse.from_result(result, timeout_s=budget).model_dump(
                    mode="json"
                )
            msg = await subscription.get(timeout_s=remaining)
            if msg is None:
                continue

            addr = _filter_address_for(session_id, msg)
            entry = cond_filter.get(session_id, addr) if addr is not None else None
            if entry is None:
                # 无条件断点（或非受管地址）：命中直接返回，行为与修复前一致。
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

            # entry 非空 ⇒ addr 非空（addr=None 时 entry=None 已提前返回）；
            # 此断言仅向类型检查器传达该不变式。
            assert addr is not None
            # 命中风暴熔断：同一地址 ≥10 次命中且相邻间隔 <1s → 撤防。
            now = time.monotonic()
            stamps = hit_stamps.setdefault(addr, [])
            stamps.append(now)
            recent = stamps[-_STORM_HITS:]
            if len(recent) == _STORM_HITS and all(
                recent[i + 1] - recent[i] < _STORM_GAP_S for i in range(_STORM_HITS - 1)
            ):
                return await _storm_break(
                    session_id=session_id,
                    address=addr,
                    entry=entry,
                    msg=msg,
                    timeout_s=budget,
                    filtered_total=filtered_total,
                )

            verdict = await _evaluate_condition(session_id, entry["condition"])
            if verdict is False:
                cond_filter.bump_filtered(session_id, addr)
                filtered_total += 1
                try:
                    async with session_client(session_id) as client:
                        await client.resume()
                except Exception as e:  # noqa: BLE001 — resume 失败则等预算耗尽
                    logger.warning("cond_filter: resume after falsy hit failed: %s", e)
                continue

            # 真命中（或求值失败时保守按命中处理，绝不静默丢命中）。
            cond_filter.bump_hit(session_id, addr)
            current = cond_filter.get(session_id, addr) or entry
            result = WaitBreakpointResult(
                hit=True,
                already_paused=False,
                pc=msg.get("pc"),
                reason=msg.get("reason"),
                related_address=msg.get("relatedAddress"),
                ticks=msg.get("ticks"),
                condition=current["condition"],
                condition_filtered=current["filtered"],
            )
            out = WaitBreakpointResponse.from_result(result, timeout_s=budget).model_dump(
                mode="json"
            )
            if verdict is None:
                out["note"] = (
                    "condition could not be evaluated (WS error or unparseable "
                    "result) — treated as a hit so a real breakpoint is never "
                    "dropped; verify the expression manually with ppsspp_evaluate"
                )
            return out
    finally:
        subscription.close()


# ── ppsspp_frame_snapshot ───────────────────────────────────────────────


@mcp.tool(
    name="ppsspp_frame_snapshot",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
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


    ROUTING: pause+capture+resume in one call -> here; cheap PC-only check -> ppsspp_query(action='register', name='pc', safe=true); recurring sampled probes -> ppsspp_state_observer.
    BEHAVIOR: STATE-CHANGE. The session lock is held for the whole call (pause→capture→resume is short). A CPU we paused is resumed before returning; an already-paused CPU stays paused. A failing capture never leaves the game frozen.

    RETURNS: {was_stepping, resumed, pc, trust_level, registers, probes} — registers/probes keys are ALWAYS present; they carry null when opted out (want_registers=false / probes omitted) — nullable-key contract, 2026-09-08."""
    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_frame_snapshot", "session_id": session_id},
    )
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
                from ppsspp_dfx_mcp.service.probe_observer import (
                    _observe_probes,
                    _resolve_target_probes,
                    _seed_from_yaml,
                )
                from ppsspp_dfx_mcp.views.state_observer import (
                    StateObserverResponse,
                )

                _seed_from_yaml(session_id)
                observation = await _observe_probes(
                    client, _resolve_target_probes(probes, session_id), 1
                )
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


    ROUTING: address access watch with capture -> here; execution breakpoint management -> ppsspp_breakpoint.
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
            "tool": "ppsspp_breakpoint",
            "action": "trace",
            "session_id": session_id,
            "address": address,
            "access": access,
        },
    )
    await validate_session_alive(session_id)
    observer = await get_live_observer(session_id)
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
                # Capture the hit counter BEFORE removal —
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
            # Shielded — a cancelled caller must not leave the temp
            # mem BP armed in PPSSPP (it would keep pausing the CPU on
            # every access long after this tool returned).
            import asyncio

            async def _cleanup():
                await _remove_mem_bp_quietly(session_id, addr)

            try:
                await asyncio.shield(_cleanup())
            except asyncio.CancelledError:
                logger.warning(
                    "trace cleanup: caller cancelled during temporary "
                    "breakpoint removal; shielded removal continues in "
                    "background"
                )
        subscription.close()
