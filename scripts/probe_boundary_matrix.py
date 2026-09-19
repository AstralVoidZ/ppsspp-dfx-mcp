"""Boundary-matrix probe for ppsspp-dfx-mcp (2026-09-06 verification round v4).

Complements scripts/verify_real_mcp.py with the boundaries the
three-phase harness does NOT cover:

- ppsspp_frame_snapshot / ppsspp_trace_memory_access — ZERO harness
  scenarios (only unit stubs + one-off probes so far).
- Prompts + Resources on the real wire (harness never touches them).
- Exact-cap boundaries (65536/65537 read, disasm count>100, wait_frames
  interval>1.0, press duration cap, mem_set size=0/64, timeout clamps).
- Leak hygiene after the boundary abuse (bp tables empty, CPU running).

Phases:
- P0: no-session surface probes (prompts/resources/param guards) — no
  PPSSPP needed.
- P1: live-session boundary matrix (boots PPSSPP via the resilient
  start, exactly like the harness).

Results: .ppsspp-dfx/output/boundary_probe/report.json. Exit 1 on any
failure. This is a DIAGNOSTIC instrument, not a regression gate — the
follow-up test-module refactor plan decides which probes become
harness scenarios.

Usage (from the repository root):
    PYTHONPATH=src python scripts/probe_boundary_matrix.py [--phase all|p0|p1]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# R-E (2026-09-08): shared runner primitives — classification, launch
# params, boot and liveness live in _wire.py (single source together
# with verify_real_mcp.py).
from _wire import (
    ISO_PATH,
    WORKSPACE_ROOT,
    _is_mcp_error,
    build_stdio_params,
    liveness_three_checks,
)
from _wire import (
    call_tool as _wire_call_tool,
)
from mcp import ClientSession
from mcp.client.stdio import stdio_client

REPORT_PATH = WORKSPACE_ROOT / ".ppsspp-dfx" / "output" / "boundary_probe" / "report.json"

SCRATCH = "0x09FE0000"
SCRATCH_INT = int(SCRATCH, 16)
GAME_MODE = "0x08A0D000"
CALL_TIMEOUT_S = 120.0

SESSION_ID_TOOLS = {
    "ppsspp_read_memory",
    "ppsspp_write_memory",
    "ppsspp_get_pc",
    "ppsspp_query",
    "ppsspp_write_register",
    "ppsspp_evaluate",
    "ppsspp_disassemble",
    "ppsspp_search_disasm",
    "ppsspp_assemble",
    "ppsspp_breakpoint",
    "ppsspp_step",
    "ppsspp_state_observer",
    "ppsspp_batch_step",
    "ppsspp_wait_frames",
    "ppsspp_press_button",
    "ppsspp_hold_buttons",
    "ppsspp_send_analog",
    "ppsspp_screenshot",
    "ppsspp_dump_texture",
    "ppsspp_dump_clut",
    "ppsspp_gpu_stats",
    "ppsspp_gpu_record",
    "ppsspp_replay",
    "ppsspp_smoke_test",
    "ppsspp_run_script",
    "ppsspp_search_memory_info",
    "ppsspp_frame_snapshot",
}


@dataclass
class Probe:
    id: str
    expect: str = "ok"  # ok | error | either
    note: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    tool: str | None = None  # plain tool call when set
    kind: str = "call"  # call | surface | composite
    fn: Callable[[ClientSession, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    # check receives the full record dict (structured = rec["structured"]);
    # it runs only on status=="ok" records and returns a problem list.
    check: Callable[[dict[str, Any]], list[str]] | None = None


def problems_for(rec: dict[str, Any], expect: str) -> list[str]:
    status = rec["status"]
    if status in ("timeout", "exception"):
        return [f"{status}: {str(rec.get('error', ''))[:200]}"]
    if expect == "ok" and status != "ok":
        return [f"expected ok, got {status}: {str(rec.get('error', ''))[:200]}"]
    if expect == "error" and status == "ok":
        return ["expected a deterministic rejection, got ok"]
    return []


def passed(rec: dict[str, Any]) -> bool:
    status = rec["status"]
    if status in ("timeout", "exception"):
        return False
    if rec["expect"] == "ok":
        return status == "ok" and not rec["problems"]
    if rec["expect"] == "error":
        return status in ("tool_error", "rpc_error") and not rec["problems"]
    return not rec["problems"]  # either


async def call(
    session: ClientSession, tool: str, args: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    """Thin delegate — the canonical implementation lives in _wire."""
    return await _wire_call_tool(
        session, tool, args, session_id=state.get("SESSION_ID"), session_tools=SESSION_ID_TOOLS
    )


async def mem_bp_addresses(session: ClientSession, state: dict[str, Any]) -> list[int]:
    r = await call(session, "ppsspp_breakpoint", {"action": "mem_list"}, state)
    s = r.get("structured") or {}
    return [int(b.get("address", 0)) for b in s.get("breakpoints", []) if isinstance(b, dict)]


# ── surface probes (prompts / resources / tools.list) ────────────────────


async def _tools_list(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    res = await asyncio.wait_for(session.list_tools(), timeout=30.0)
    names = sorted(t.name for t in res.tools)
    return {
        "status": "ok",
        "error": "",
        "text": "",
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "structured": {"tool_count": len(names), "names": names},
    }


def _check_tools_list(rec: dict[str, Any]) -> list[str]:
    s = rec.get("structured") or {}
    names = s.get("names", [])
    missing = [
        t
        for t in ("ppsspp_frame_snapshot", "ppsspp_trace_memory_access", "ppsspp_wait_breakpoint")
        if t not in names
    ]
    if s.get("tool_count", 0) < 38:
        return [f"tool_count={s.get('tool_count')} < 38"]
    return [f"missing tools: {missing}"] if missing else []


async def _prompts_list(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        res = await asyncio.wait_for(session.list_prompts(), timeout=30.0)
    except Exception as e:  # noqa: BLE001
        status = "rpc_error" if _is_mcp_error(e) else "exception"
        return {
            "status": status,
            "error": f"{type(e).__name__}: {e}"[:300],
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "structured": None,
            "text": "",
        }
    names = sorted(p.name for p in res.prompts)
    return {
        "status": "ok",
        "error": "",
        "text": "",
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "structured": {"prompts": names},
    }


def _prompt_get(name: str, args: dict[str, Any] | None) -> Callable:
    async def f(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(session.get_prompt(name, args), timeout=30.0)
        except Exception as e:  # noqa: BLE001
            status = "rpc_error" if _is_mcp_error(e) else "exception"
            return {
                "status": status,
                "error": f"{type(e).__name__}: {e}"[:300],
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "structured": None,
                "text": "",
            }
        msgs = res.messages
        text = "\n".join(str(m.content) for m in msgs)[:1200]
        return {
            "status": "ok" if msgs else "tool_error",
            "error": "" if msgs else "empty messages list",
            "text": text,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "structured": {"n_messages": len(msgs), "roles": [str(m.role) for m in msgs]},
        }

    return f


async def _resources_list(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        res = await asyncio.wait_for(session.list_resources(), timeout=30.0)
    except Exception as e:  # noqa: BLE001
        status = "rpc_error" if _is_mcp_error(e) else "exception"
        return {
            "status": status,
            "error": f"{type(e).__name__}: {e}"[:300],
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "structured": None,
            "text": "",
        }
    uris = sorted(str(r.uri) for r in res.resources)
    return {
        "status": "ok",
        "error": "",
        "text": "",
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        "structured": {"resources": uris},
    }


def _resource_read(uri: str) -> Callable:
    """No-session read: the server must REJECT (requires exactly one
    session). A rejection is an McpError (rpc_error) or an error
    response; a payload means the guard is broken and the probe fails
    via problems_for(expect='error')."""

    async def f(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
        t0 = time.perf_counter()
        try:
            res = await asyncio.wait_for(session.read_resource(uri), timeout=30.0)
        except Exception as e:  # noqa: BLE001
            status = "rpc_error" if _is_mcp_error(e) else "exception"
            return {
                "status": status,
                "error": f"{type(e).__name__}: {e}"[:300],
                "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                "structured": None,
                "text": "",
            }
        texts = [str(c) for c in res.contents]
        return {
            "status": "ok",
            "error": "",
            "text": texts[0][:300] if texts else "",
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "structured": {"n_contents": len(res.contents)},
        }

    return f


# ── composite probes ─────────────────────────────────────────────────────


async def _tr_read_hit(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    leak_before = await mem_bp_addresses(session, state)
    r = await call(
        session,
        "ppsspp_trace_memory_access",
        {"address": GAME_MODE, "access": "read", "size": 4, "timeout_s": 20.0},
        state,
    )
    leak_after = await mem_bp_addresses(session, state)
    new_leak = [a for a in leak_after if a not in leak_before]
    ok = r["status"] == "ok" and (r["structured"] or {}).get("hit") is True
    return {
        "status": "ok" if ok else "tool_error",
        "error": "" if ok else r["error"][:300],
        "text": r["text"][:400],
        "latency_ms": r["latency_ms"],
        "structured": {
            "trace": r["structured"],
            "leaked_bp": new_leak,
            "pre_existing": leak_before,
        },
    }


def _check_tr_hit(rec: dict[str, Any]) -> list[str]:
    s = rec.get("structured") or {}
    tr = s.get("trace") or {}
    p: list[str] = []
    if tr.get("bp_removed") is not True:
        p.append(f"bp_removed={tr.get('bp_removed')!r}")
    if tr.get("resumed") is not True:
        p.append(f"resumed={tr.get('resumed')!r}")
    hits = tr.get("hits") or []
    if not hits:
        p.append("hits empty")
    else:
        h = hits[0]
        if not str(h.get("pc", "")).startswith("0x"):
            p.append(f"hit pc not hex-shaped: {h.get('pc')!r}")
        if int(h.get("mem_hits", 0)) < 1:
            p.append(f"mem_hits={h.get('mem_hits')!r} (S3 attribution absent)")
    if s.get("leaked_bp"):
        p.append(f"leaked mem breakpoints: {[hex(a) for a in s['leaked_bp']]}")
    return p


async def _tr_read_hit_full(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    leak_before = await mem_bp_addresses(session, state)
    r = await call(
        session,
        "ppsspp_trace_memory_access",
        {
            "address": GAME_MODE,
            "access": "read",
            "size": 4,
            "timeout_s": 20.0,
            "want_registers": True,
            "want_backtrace": True,
        },
        state,
    )
    leak_after = await mem_bp_addresses(session, state)
    new_leak = [a for a in leak_after if a not in leak_before]
    ok = r["status"] == "ok" and (r["structured"] or {}).get("hit") is True
    return {
        "status": "ok" if ok else "tool_error",
        "error": "" if ok else r["error"][:300],
        "text": "",
        "latency_ms": r["latency_ms"],
        "structured": {
            "trace": r["structured"],
            "leaked_bp": new_leak,
            "pre_existing": leak_before,
        },
    }


def _check_tr_full(rec: dict[str, Any]) -> list[str]:
    p = _check_tr_hit(rec)
    s = rec.get("structured") or {}
    hits = (s.get("trace") or {}).get("hits") or []
    if hits:
        if "registers" not in hits[0]:
            p.append("want_registers=true but no registers key")
        if "backtrace" not in hits[0]:
            p.append("want_backtrace=true but no backtrace key")
    return p


async def _tr_timeout_clean(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    leak_before = await mem_bp_addresses(session, state)
    r = await call(
        session,
        "ppsspp_trace_memory_access",
        {"address": "0x09FF8000", "access": "read", "size": 4, "timeout_s": 0.1},
        state,
    )
    leak_after = await mem_bp_addresses(session, state)
    leak = [a for a in leak_after if a not in leak_before]
    s = r.get("structured") or {}
    ok = r["status"] == "ok" and s.get("hit") is False and s.get("bp_removed") is True
    return {
        "status": "ok" if ok else "tool_error",
        "error": ""
        if ok
        else f"status={r['status']} hit={s.get('hit')!r} "
        f"bp_removed={s.get('bp_removed')!r}: {r['error'][:200]}",
        "text": "",
        "latency_ms": r["latency_ms"],
        "structured": {"trace": r["structured"], "leaked_bp": leak},
    }


def _check_tr_timeout(rec: dict[str, Any]) -> list[str]:
    s = rec.get("structured") or {}
    tr = s.get("trace") or {}
    p: list[str] = []
    if tr.get("hit") is not False:
        p.append(f"hit={tr.get('hit')!r} (scratch address must not be hit)")
    if tr.get("bp_removed") is not True:
        p.append(f"bp_removed={tr.get('bp_removed')!r} (timeout must clean up)")
    if s.get("leaked_bp"):
        p.append(f"leaked mem breakpoints: {[hex(a) for a in s['leaked_bp']]}")
    return p


async def _wb_already_paused(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    r_pause = await call(session, "ppsspp_step", {"action": "pause"}, state)
    r_wb = await call(session, "ppsspp_wait_breakpoint", {"timeout_s": 2.0}, state)
    r_resume = await call(session, "ppsspp_step", {"action": "resume"}, state)
    s = r_wb.get("structured") or {}
    ok = (
        r_pause["status"] == "ok"
        and r_wb["status"] == "ok"
        and s.get("hit") is True
        and s.get("already_paused") is True
        and r_resume["status"] == "ok"
    )
    return {
        "status": "ok" if ok else "tool_error",
        "error": ""
        if ok
        else f"pause={r_pause['status']} wb={r_wb['status']}:{r_wb['error'][:150]} "
        f"resume={r_resume['status']}",
        "text": "",
        "latency_ms": r_wb["latency_ms"],
        "structured": {"wb": s, "pause": r_pause["status"], "resume": r_resume["status"]},
    }


def _check_wb_paused(rec: dict[str, Any]) -> list[str]:
    wb = (rec.get("structured") or {}).get("wb") or {}
    p: list[str] = []
    if wb.get("hit") is not True:
        p.append(f"hit={wb.get('hit')!r} (already-paused must short-circuit)")
    if wb.get("already_paused") is not True:
        p.append(f"already_paused={wb.get('already_paused')!r}")
    if not str(wb.get("pc", "")).startswith("0x"):
        p.append(f"pc not hex-shaped: {wb.get('pc')!r}")
    return p


async def _tr_already_paused(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    leak_before = await mem_bp_addresses(session, state)
    r_pause = await call(session, "ppsspp_step", {"action": "pause"}, state)
    r_tr = await call(
        session,
        "ppsspp_trace_memory_access",
        {"address": GAME_MODE, "access": "read", "size": 4, "timeout_s": 2.0},
        state,
    )
    r_resume = await call(session, "ppsspp_step", {"action": "resume"}, state)
    leak_after = await mem_bp_addresses(session, state)
    leak = [a for a in leak_after if a not in leak_before]
    s = r_tr.get("structured") or {}
    ok = (
        r_pause["status"] == "ok"
        and r_tr["status"] == "ok"
        and s.get("already_paused") is True
        and s.get("hit") is False
        and r_resume["status"] == "ok"
    )
    return {
        "status": "ok" if ok else "tool_error",
        "error": ""
        if ok
        else f"pause={r_pause['status']} tr={r_tr['status']}:{r_tr['error'][:200]} "
        f"resume={r_resume['status']}",
        "text": "",
        "latency_ms": r_tr["latency_ms"],
        "structured": {
            "trace": s,
            "leaked_bp": leak,
            "pause": r_pause["status"],
            "resume": r_resume["status"],
        },
    }


def _check_tr_paused(rec: dict[str, Any]) -> list[str]:
    s = rec.get("structured") or {}
    tr = s.get("trace") or {}
    p: list[str] = []
    if tr.get("already_paused") is not True:
        p.append(f"already_paused={tr.get('already_paused')!r}")
    if tr.get("hit") is not False:
        p.append(f"hit={tr.get('hit')!r} (paused CPU cannot hit)")
    if s.get("leaked_bp"):
        p.append(f"leaked mem breakpoints: {[hex(a) for a in s['leaked_bp']]}")
    return p


async def _fs_running(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    r = await call(session, "ppsspp_frame_snapshot", {}, state)
    s = r.get("structured") or {}
    regs = s.get("registers")
    ok = (
        r["status"] == "ok"
        and s.get("was_stepping") is False
        and s.get("resumed") is True
        and str(s.get("pc", "")).startswith("0x")
        and isinstance(regs, dict)
        and len(regs) > 0
    )
    return {
        "status": "ok" if ok else "tool_error",
        "error": ""
        if ok
        else f"status={r['status']} was_stepping={s.get('was_stepping')!r} "
        f"resumed={s.get('resumed')!r} pc={s.get('pc')!r} "
        f"regs={type(regs).__name__}:{len(regs) if isinstance(regs, dict) else '-'}",
        "text": "",
        "latency_ms": r["latency_ms"],
        "structured": {
            "snapshot": {
                k: (f"<{len(v)} entries>" if k == "registers" else v) for k, v in s.items()
            }
        },
    }


def _check_fs_running(rec: dict[str, Any]) -> list[str]:
    snap = (rec.get("structured") or {}).get("snapshot") or {}
    p: list[str] = []
    if snap.get("resumed") is not True:
        p.append(f"resumed={snap.get('resumed')!r} (running CPU must be resumed)")
    if snap.get("was_stepping") is not False:
        p.append(f"was_stepping={snap.get('was_stepping')!r}")
    if not str(snap.get("pc", "")).startswith("0x"):
        p.append(f"pc not hex-shaped: {snap.get('pc')!r}")
    return p


async def _fs_while_paused(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    r_pause = await call(session, "ppsspp_step", {"action": "pause"}, state)
    r_fs = await call(session, "ppsspp_frame_snapshot", {}, state)
    s = r_fs.get("structured") or {}
    still_paused_ok = (
        r_fs["status"] == "ok" and s.get("was_stepping") is True and s.get("resumed") is False
    )
    r_resume = await call(session, "ppsspp_step", {"action": "resume"}, state)
    ok = r_pause["status"] == "ok" and still_paused_ok and r_resume["status"] == "ok"
    return {
        "status": "ok" if ok else "tool_error",
        "error": ""
        if ok
        else f"pause={r_pause['status']} snap={r_fs['status']} "
        f"was_stepping={s.get('was_stepping')!r} "
        f"resumed={s.get('resumed')!r} resume={r_resume['status']}",
        "text": "",
        "latency_ms": r_fs["latency_ms"],
        "structured": {
            "snapshot": {k: v for k, v in s.items() if k != "registers"},
            "pause": r_pause["status"],
            "resume": r_resume["status"],
        },
    }


def _check_fs_paused(rec: dict[str, Any]) -> list[str]:
    snap = (rec.get("structured") or {}).get("snapshot") or {}
    p: list[str] = []
    if snap.get("was_stepping") is not True:
        p.append(f"was_stepping={snap.get('was_stepping')!r}")
    if snap.get("resumed") is not False:
        p.append(f"resumed={snap.get('resumed')!r} (already-paused CPU must STAY paused)")
    return p


async def _fs_probes_known(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    r_reg = await call(
        session,
        "ppsspp_state_observer",
        {
            "action": "register",
            "name": "bnd_probe",
            "address": SCRATCH,
            "size": 4,
            "description": "boundary probe",
        },
        state,
    )
    r_w = await call(
        session,
        "ppsspp_write_memory",
        {"address": SCRATCH, "data": "0x5A5A5A5A", "format": "u32"},
        state,
    )
    r_fs = await call(session, "ppsspp_frame_snapshot", {"probes": "bnd_probe"}, state)
    r_clear = await call(
        session, "ppsspp_state_observer", {"action": "clear", "name": "bnd_probe"}, state
    )
    probes = (r_fs.get("structured") or {}).get("probes") or {}
    flat = json.dumps(probes, default=str)
    ok = (
        r_reg["status"] == "ok"
        and r_w["status"] == "ok"
        and r_fs["status"] == "ok"
        and "bnd_probe" in flat
        and r_clear["status"] == "ok"
    )
    return {
        "status": "ok" if ok else "tool_error",
        "error": ""
        if ok
        else f"reg={r_reg['status']} w={r_w['status']} fs={r_fs['status']} "
        f"clear={r_clear['status']} probes={flat[:200]}",
        "text": "",
        "latency_ms": r_fs["latency_ms"],
        "structured": {
            "probes_section": probes,
            "register": r_reg["status"],
            "write": r_w["status"],
            "clear": r_clear["status"],
        },
    }


def _check_fs_probes(rec: dict[str, Any]) -> list[str]:
    probes = (rec.get("structured") or {}).get("probes_section") or {}
    if not probes:
        return ["snapshot carried no probes section"]
    if "bnd_probe" not in json.dumps(probes, default=str):
        return [f"bnd_probe missing from probes section: {json.dumps(probes, default=str)[:200]}"]
    return []


async def _bp_mem_set_sizes(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    r0 = await call(
        session, "ppsspp_breakpoint", {"action": "mem_set", "address": SCRATCH, "size": 0}, state
    )
    r64 = await call(
        session, "ppsspp_breakpoint", {"action": "mem_set", "address": SCRATCH, "size": 64}, state
    )
    # remove EVERY entry at SCRATCH (PPSSPP allows overlapping memchecks;
    # the accepted size=0 probe above may have stacked a second entry)
    listed: list[dict[str, Any]] = []
    r_list = await call(session, "ppsspp_breakpoint", {"action": "mem_list"}, state)
    listed = [
        b
        for b in (r_list.get("structured") or {}).get("breakpoints", [])
        if isinstance(b, dict) and int(b.get("address", 0)) == SCRATCH_INT
    ]
    remove_status: list[str] = []
    for _ in range(8):
        if not listed:
            break
        b0 = listed[0]
        r_rm = await call(
            session,
            "ppsspp_breakpoint",
            {"action": "mem_remove", "address": SCRATCH, "size": int(b0.get("size", 4))},
            state,
        )
        remove_status.append("size={}:{}".format(b0.get("size"), r_rm["status"]))
        r_list = await call(session, "ppsspp_breakpoint", {"action": "mem_list"}, state)
        listed = [
            b
            for b in (r_list.get("structured") or {}).get("breakpoints", [])
            if isinstance(b, dict) and int(b.get("address", 0)) == SCRATCH_INT
        ]
    after = await mem_bp_addresses(session, state)
    zero_rejected = r0["status"] == "tool_error"
    armed_and_removed = not listed
    ok = zero_rejected and armed_and_removed and not after
    return {
        "status": "ok" if ok else "tool_error",
        "error": ""
        if ok
        else f"zero={r0['status']}:{r0['error'][:120]} "
        f"size64={r64['status']}:{r64['error'][:120]} "
        f"removes={remove_status} leak={after}",
        "text": (r0["text"][:150] + " || " + r64["text"][:150]),
        "latency_ms": round(r0["latency_ms"] + r64["latency_ms"], 1),
        "structured": {
            "zero_size_status": r0["status"],
            "zero_size_error": r0["error"][:150],
            "size64_status": r64["status"],
            "remove_attempts": remove_status,
            "unremoved_entries": len(listed),
            "leaked_bp": after,
        },
    }


def _check_bp_sizes(rec: dict[str, Any]) -> list[str]:
    s = rec.get("structured") or {}
    p: list[str] = []
    if s.get("zero_size_status") != "tool_error":
        p.append(
            f"mem_set size=0 accepted ({s.get('zero_size_status')}) — "
            f"zero-width watch is meaningless, expect rejection"
        )
    if s.get("leaked_bp"):
        p.append(f"leaked mem breakpoints: {[hex(a) for a in s['leaked_bp']]}")
    return p


async def _rp_roundtrip(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    r_begin = await call(session, "ppsspp_replay", {"action": "begin"}, state)
    await asyncio.sleep(2.0)  # give the recorder frames to capture
    r_status = await call(session, "ppsspp_replay", {"action": "status"}, state)
    st = r_status.get("structured") or {}
    r_save = await call(
        session, "ppsspp_replay", {"action": "save", "file_path": "bnd_rp_roundtrip.ppr"}, state
    )
    save_meta = r_save.get("structured") or {}
    save_size = int(save_meta.get("size", 0) or 0)
    r_abort1 = await call(session, "ppsspp_replay", {"action": "abort"}, state)
    # load needs a fresh replay context after the abort
    r_begin2 = await call(session, "ppsspp_replay", {"action": "begin"}, state)
    r_load = {"status": "skipped", "error": "", "latency_ms": 0.0, "structured": None, "text": ""}
    if r_save["status"] == "ok" and save_size > 0:
        r_load = await call(
            session, "ppsspp_replay", {"action": "load", "file_path": "bnd_rp_roundtrip.ppr"}, state
        )
    r_abort2 = await call(session, "ppsspp_replay", {"action": "abort"}, state)
    ok = (
        r_begin["status"] == "ok"
        and r_save["status"] == "ok"
        and save_size > 0
        and r_load["status"] in ("ok", "skipped")
        and r_begin2["status"] == "ok"
        and r_abort1["status"] == "ok"
        and r_abort2["status"] == "ok"
    )
    return {
        "status": "ok" if ok else "tool_error",
        "error": ""
        if ok
        else f"begin={r_begin['status']} status_executing="
        f"{st.get('executing')!r} status_saving={st.get('saving')!r} "
        f"save={r_save['status']}:size={save_size} "
        f"load={r_load['status']}:{r_load['error'][:100]} "
        f"begin2={r_begin2['status']} "
        f"aborts={r_abort1['status']}/{r_abort2['status']}",
        "text": r_save["text"][:200],
        "latency_ms": round(
            r_begin["latency_ms"]
            + r_status["latency_ms"]
            + r_save["latency_ms"]
            + r_load["latency_ms"]
            + r_begin2["latency_ms"],
            1,
        ),
        "structured": {
            "begin": r_begin["status"],
            "status": {k: st.get(k) for k in ("executing", "saving", "version")},
            "save": r_save["status"],
            "save_size": save_size,
            "load": r_load["status"],
            "begin2": r_begin2["status"],
            "aborts": [r_abort1["status"], r_abort2["status"]],
            "save_meta": save_meta,
        },
    }


async def _la_sections_all(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    r = await call(session, "ppsspp_list_addresses", {}, state)
    s = r.get("structured") or {}
    sections = s.get("sections") or {}
    if isinstance(sections, dict):
        names = sorted(sections.keys())
    elif isinstance(sections, list):
        names = [x.get("name") for x in sections if isinstance(x, dict)]
    else:
        names = []
    results: dict[str, str] = {}
    for n in names:
        rr = await call(session, "ppsspp_list_addresses", {"section": n}, state)
        results[str(n)] = rr["status"]
    bad = [n for n, st in results.items() if st != "ok"]
    return {
        "status": "ok" if (r["status"] == "ok" and not bad) else "tool_error",
        "error": "" if not bad else f"sections failed: {bad}",
        "text": "",
        "latency_ms": r["latency_ms"],
        "structured": {
            "default_keys": list(s.keys()),
            "sections": results or {"(none discovered)": ""},
        },
    }


async def _hygiene_final(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    leak_bp = await mem_bp_addresses(session, state)
    r_obs = await call(session, "ppsspp_state_observer", {"action": "list"}, state)
    flat_obs = json.dumps(r_obs.get("structured") or {}, default=str)
    leaked_obs = "bnd_probe" in flat_obs
    # Liveness: the title screen parks the PC in an idle loop, so a
    # pc-delta sample is unreliable — use the smoke battery instead
    # (it includes the CPU-running check).
    r_smoke = await call(session, "ppsspp_smoke_test", {}, state)
    smoke = r_smoke.get("structured") or {}
    # R-C three-check liveness judge from the shared runner lib
    # (game_mode_valid excluded — game-phase dependent, F-07).
    cpu_running, checks = liveness_three_checks(smoke)
    ok = not leak_bp and not leaked_obs and cpu_running
    return {
        "status": "ok" if ok else "tool_error",
        "error": ""
        if ok
        else f"bp_leak={leak_bp} obs_leak={leaked_obs} "
        f"smoke={smoke.get('overall_status')!r} checks={checks}",
        "text": "",
        "latency_ms": r_smoke["latency_ms"],
        "structured": {
            "leaked_bp": leak_bp,
            "observer_list_flat": flat_obs[:200],
            "leaked_observer_bnd_probe": leaked_obs,
            "cpu_running": cpu_running,
            "smoke_overall": smoke.get("overall_status"),
            "smoke_checks": checks,
        },
    }


def _check_hygiene(rec: dict[str, Any]) -> list[str]:
    s = rec.get("structured") or {}
    p: list[str] = []
    if s.get("leaked_bp"):
        p.append(f"leaked mem breakpoints: {[hex(a) for a in s['leaked_bp']]}")
    if s.get("leaked_observer_bnd_probe"):
        p.append("leaked observer probe: bnd_probe")
    if s.get("cpu_running") is not True:
        p.append(f"cpu not running after probes (smoke_overall={s.get('smoke_overall')!r})")
    return p


async def _pre_clean(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    """R17 parity: stop stale sessions left by a previous run BEFORE the
    guard probes — a leftover session makes the no-session guard and the
    resource single-session guard non-deterministic (verified live:
    the first P0 run bound to a leftover PPSSPP and served real data)."""
    r = await call(session, "ppsspp_session_list", {}, state)
    sids = [
        sess.get("session_id")
        for sess in (r.get("structured") or {}).get("sessions", [])
        if isinstance(sess, dict) and sess.get("session_id")
    ]
    stopped: list[str] = []
    for sid in sids:
        rs = await call(session, "ppsspp_session", {"action": "stop", "session_id": sid}, state)
        if rs["status"] == "ok":
            stopped.append(str(sid))
    return {
        "status": "ok",
        "error": "",
        "text": "",
        "latency_ms": r.get("latency_ms", 0.0),
        "structured": {"found": len(sids), "stopped": stopped},
    }


async def _hold_combo(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    r_hold = await call(session, "ppsspp_hold_buttons", {"buttons": "cross|square"}, state)
    r_rel = await call(session, "ppsspp_hold_buttons", {"buttons": ""}, state)
    ok = r_hold["status"] == "ok" and r_rel["status"] == "ok"
    return {
        "status": "ok" if ok else "tool_error",
        "error": ""
        if ok
        else f"hold={r_hold['status']}:{r_hold['error'][:120]} "
        f"release={r_rel['status']}:{r_rel['error'][:120]}",
        "text": "",
        "latency_ms": round(r_hold["latency_ms"], 1),
        "structured": {"hold": r_hold["status"], "release": r_rel["status"]},
    }


async def _gpu_double(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    r1 = await call(session, "ppsspp_gpu_record", {}, state)
    r2 = await call(session, "ppsspp_gpu_record", {}, state)
    ok = r1["status"] == "ok" and r2["status"] == "ok"
    return {
        "status": "ok" if ok else "tool_error",
        "error": ""
        if ok
        else f"r1={r1['status']}:{r1['error'][:100]} r2={r2['status']}:{r2['error'][:100]}",
        "text": "",
        "latency_ms": round(r1["latency_ms"] + r2["latency_ms"], 1),
        "structured": {"first": r1["structured"], "second": r2["structured"]},
    }


PROBES_P0: list[Probe] = [
    Probe(
        "P0.pre_clean",
        kind="composite",
        fn=_pre_clean,
        note="stop stale sessions from previous runs — guard probes "
        "downstream need a deterministic zero-session state",
    ),
    Probe(
        "P0.tools.list",
        kind="composite",
        fn=_tools_list,
        check=_check_tools_list,
        note="tool surface incl. the 2 zero-coverage tools",
    ),
    Probe(
        "P0.prompts.list",
        kind="composite",
        fn=_prompts_list,
        check=lambda rec: (
            (
                ["memory-trace-wizard missing from prompts/list"]
                if "memory-trace-wizard" not in (rec.get("structured") or {}).get("prompts", [])
                else []
            )
            + (
                ["memory-breakpoint-wizard missing from prompts/list"]
                if "memory-breakpoint-wizard"
                not in (rec.get("structured") or {}).get("prompts", [])
                else []
            )
        ),
    ),
    Probe(
        "P0.prompts.get_trace_wizard",
        kind="composite",
        fn=_prompt_get("memory-trace-wizard", {"address": GAME_MODE}),
        check=lambda rec: (
            ["prompt text does not mention trace_memory_access"]
            if "trace_memory_access" not in rec.get("text", "")
            else []
        ),
        note="content-quality gate: prompt must route to the H1 tool",
    ),
    Probe(
        "P0.prompts.get_bp_wizard",
        kind="composite",
        fn=_prompt_get("memory-breakpoint-wizard", {"address": SCRATCH}),
    ),
    Probe(
        "P0.prompts.get_trace_wizard.noargs",
        kind="composite",
        fn=_prompt_get("memory-trace-wizard", None),
        expect="either",
        note="address is a required prompt arg — reject or default, record",
    ),
    Probe(
        "P0.prompts.get_unknown",
        kind="composite",
        fn=_prompt_get("no_such_wizard", None),
        expect="error",
    ),
    Probe(
        "P0.resources.list",
        kind="composite",
        fn=_resources_list,
        check=lambda rec: (
            (
                ["ppsspp://game-state missing"]
                if "ppsspp://game-state" not in (rec.get("structured") or {}).get("resources", [])
                else []
            )
            + (
                ["ppsspp://registers missing"]
                if "ppsspp://registers" not in (rec.get("structured") or {}).get("resources", [])
                else []
            )
        ),
    ),
    Probe(
        "P0.resources.read.no_session",
        kind="composite",
        fn=_resource_read("ppsspp://game-state"),
        expect="error",
        note="requires exactly one session — none exists in P0",
    ),
    Probe(
        "P0.wb.no_session",
        tool="ppsspp_wait_breakpoint",
        args={"session_id": "sess_unknown_guard", "timeout_s": 0.5},
        expect="error",
    ),
    Probe(
        "P0.fs.no_session",
        tool="ppsspp_frame_snapshot",
        args={"session_id": "sess_unknown_guard"},
        expect="error",
    ),
    Probe(
        "P0.tr.addr_zero",
        tool="ppsspp_trace_memory_access",
        args={"session_id": "sess_unknown_guard", "address": "0x0"},
        expect="error",
        note="must reject BEFORE session validation",
    ),
    Probe(
        "P0.tr.size_3",
        tool="ppsspp_trace_memory_access",
        args={"session_id": "sess_unknown_guard", "address": GAME_MODE, "size": 3},
        expect="error",
    ),
    Probe(
        "P0.tr.size_8",
        tool="ppsspp_trace_memory_access",
        args={"session_id": "sess_unknown_guard", "address": GAME_MODE, "size": 8},
        expect="error",
    ),
    Probe(
        "P0.tr.bad_hex",
        tool="ppsspp_trace_memory_access",
        args={"session_id": "sess_unknown_guard", "address": "nothex"},
        expect="error",
    ),
    Probe(
        "P0.tr.valid_no_session",
        tool="ppsspp_trace_memory_access",
        args={"session_id": "sess_unknown_guard", "address": GAME_MODE},
        expect="error",
        note="valid params + dead session → SESSION_NOT_FOUND",
    ),
]

PROBES_P1: list[Probe] = [
    # ── memory exact-cap boundaries ──
    Probe(
        "P1.mem.read_exact_cap",
        tool="ppsspp_read_memory",
        args={"action": "read_bytes", "address": "0x08804000", "size": 65536},
        check=lambda rec: (
            [f"value length {len((rec.get('structured') or {}).get('value') or [])} != 65536"]
            if len((rec.get("structured") or {}).get("value") or []) != 65536
            else []
        ),
        note="65536 is the W1 single-read cap — accept boundary",
    ),
    Probe(
        "P1.mem.read_over_cap",
        tool="ppsspp_read_memory",
        args={"action": "read_bytes", "address": "0x08804000", "size": 65537},
        expect="error",
        note="65537 = first rejected size (harness only probes 1MiB)",
    ),
    Probe(
        "P1.mem.read_top_byte",
        tool="ppsspp_read_memory",
        args={"action": "read_bytes", "address": "0x09FFFFFF", "size": 1},
        note="last valid user-RAM byte",
    ),
    Probe(
        "P1.mem.read_beyond_top",
        tool="ppsspp_read_memory",
        args={"action": "read_bytes", "address": "0x0A000000", "size": 4},
        expect="either",
        note="0x0A000000 unmapped — record semantics",
    ),
    Probe(
        "P1.mem.read_string_top",
        tool="ppsspp_read_memory",
        args={"action": "read_string", "address": "0x09FFFFF0", "max_len": 16},
        note="bounded read must not strnlen across the RAM top",
    ),
    Probe(
        "P1.mem.scan_odd_pattern",
        tool="ppsspp_read_memory",
        args={
            "action": "scan",
            "pattern": "ABC",
            "start_addr": "0x08804000",
            "end_addr": "0x08808000",
        },
        expect="error",
        note="odd nibble count must be rejected",
    ),
    Probe(
        "P1.mem.scan_no_match",
        tool="ppsspp_read_memory",
        args={
            "action": "scan",
            "pattern": "CAFEBABE00",
            "start_addr": "0x08804000",
            "end_addr": "0x08808000",
        },
        note="empty-hit scan is an ok result, not an error",
    ),
    # ── disasm / evaluate boundaries ──
    Probe(
        "P1.dis.count_over_cap",
        tool="ppsspp_disassemble",
        args={"address": "0x08804000", "count": 250},
        check=lambda rec: (
            [
                f"returned more than 100 instructions — silent clamp "
                f"contract check: "
                f"{len((rec.get('structured') or {}).get('instructions') or [])}"
            ]
            if len((rec.get("structured") or {}).get("instructions") or []) > 100
            else []
        ),
        note="_MAX_DISASM_COUNT=100 is a documented silent clamp",
    ),
    Probe(
        "P1.sd.no_match_huge_max",
        tool="ppsspp_search_disasm",
        args={"address": "0x08804000", "match": "zzzz_no_such_insn_zzz", "max_results": 100000},
        note="loop-detect must bound the scan when max_results is huge",
    ),
    Probe("P1.ev.expr_arith", tool="ppsspp_evaluate", args={"expression": "r3+0x10"}),
    Probe(
        "P1.ev.expr_dollar",
        tool="ppsspp_evaluate",
        args={"expression": "$a0"},
        expect="either",
        note="PPSSPP evaluator $reg syntax — record",
    ),
    Probe(
        "P1.ev.expr_shift",
        tool="ppsspp_evaluate",
        args={"expression": "(pc+4)>>2"},
        expect="either",
        note="parenthesised shift — grammar depth record",
    ),
    # ── breakpoint boundaries ──
    Probe(
        "P1.bp.set_addr_zero",
        tool="ppsspp_breakpoint",
        args={"action": "set", "address": "0x0"},
        expect="error",
    ),
    Probe(
        "P1.bp.mem_set_sizes",
        kind="composite",
        fn=_bp_mem_set_sizes,
        check=_check_bp_sizes,
        note="size=0 reject + size=64 semantics + real-size cleanup",
    ),
    # ── wait_breakpoint clamp + already-paused ──
    Probe(
        "P1.wb.clamp_min",
        tool="ppsspp_wait_breakpoint",
        args={"timeout_s": 0.1},
        check=lambda rec: (
            [
                f"latency {rec.get('latency_ms')}ms < 500ms — timeout_s=0.1 "
                f"was NOT clamped to the 0.5 floor"
            ]
            if rec.get("latency_ms", 0) < 450
            else []
        ),
        note="clamp floor 0.5s: latency must reflect the clamped budget",
    ),
    Probe(
        "P1.wb.already_paused",
        kind="composite",
        fn=_wb_already_paused,
        check=_check_wb_paused,
        note="pause→wait must short-circuit hit=true/already_paused=true",
    ),
    # ── trace_memory_access full matrix ──
    Probe(
        "P1.tr.read_hit",
        kind="composite",
        fn=_tr_read_hit,
        check=_check_tr_hit,
        note="game_mode read trap — S3 mem_hits + cleanup contract",
    ),
    Probe(
        "P1.tr.read_hit_full",
        kind="composite",
        fn=_tr_read_hit_full,
        check=_check_tr_full,
        note="want_registers+want_backtrace capture options",
    ),
    Probe(
        "P1.tr.timeout_clean",
        kind="composite",
        fn=_tr_timeout_clean,
        check=_check_tr_timeout,
        note="never-hit address: hit=false + bp_removed=true + no leak",
    ),
    Probe(
        "P1.tr.already_paused",
        kind="composite",
        fn=_tr_already_paused,
        check=_check_tr_paused,
        note="paused CPU: short-circuit, bp_removed=false, no leak",
    ),
    Probe(
        "P1.tr.write_access",
        tool="ppsspp_trace_memory_access",
        args={"address": GAME_MODE, "access": "write", "size": 4, "timeout_s": 0.5},
        expect="either",
        note="write trap on a mostly-read address — either path valid",
    ),
    # ── frame_snapshot matrix ──
    Probe("P1.fs.running", kind="composite", fn=_fs_running, check=_check_fs_running),
    Probe(
        "P1.fs.want_no_regs",
        tool="ppsspp_frame_snapshot",
        args={"want_registers": False},
        check=lambda rec: (
            [
                "registers populated despite want_registers=false: "
                f"{(rec.get('structured') or {}).get('registers')!r}"
            ]
            if ((rec.get("structured") or {}).get("registers") not in (None, {}, []))
            else []
        ),
        note="view contract: registers key carries null when opted out "
        "(nullable field, not omitted)",
    ),
    Probe(
        "P1.fs.probes_known",
        kind="composite",
        fn=_fs_probes_known,
        check=_check_fs_probes,
        note="observer-registered probe captured via snapshot probes=",
    ),
    Probe(
        "P1.fs.probes_unknown",
        tool="ppsspp_frame_snapshot",
        args={"probes": "no_such_probe_xyz"},
        expect="error",
        note="unknown probe name must be rejected (and CPU resumed)",
    ),
    Probe(
        "P1.fs.while_paused",
        kind="composite",
        fn=_fs_while_paused,
        check=_check_fs_paused,
        note="already-paused CPU must STAY paused (resumed=false)",
    ),
    # ── wait_frames / batch_step / input boundaries ──
    Probe(
        "P1.wf.interval_over_max",
        tool="ppsspp_wait_frames",
        args={"frames": 1, "interval": 2.0},
        expect="error",
        note="interval cap is 1.0s (harness only probes interval=0)",
    ),
    Probe(
        "P1.bs.press_over_cap",
        tool="ppsspp_batch_step",
        args={"steps": [{"type": "press", "button": "cross", "duration": 20000}]},
        expect="error",
        note="duration cap 18000 (harness never probes the cap)",
    ),
    Probe(
        "P1.bs.zero_frame_120",
        tool="ppsspp_batch_step",
        args={"steps": [{"type": "wait", "frames": 0}] * 120},
        note="no step-count cap — 120 zero-frame waits must be fast",
    ),
    Probe(
        "P1.in.press_zero",
        tool="ppsspp_press_button",
        args={"button": "cross", "duration": 0},
        expect="either",
        note="duration=0 semantics — record",
    ),
    Probe(
        "P1.in.analog_zero",
        tool="ppsspp_send_analog",
        args={"x": 0, "y": 0},
        note="corner 0,0 (harness only probes 200/100)",
    ),
    Probe(
        "P1.in.analog_max",
        tool="ppsspp_send_analog",
        args={"x": 255, "y": 255},
        note="corner 255,255 (range boundary)",
    ),
    Probe(
        "P1.in.hold_combo", kind="composite", fn=_hold_combo, note="multi-button hold + release-all"
    ),
    # ── observer semantics ──
    Probe(
        "P1.ob.samples_zero",
        tool="ppsspp_state_observer",
        args={"action": "observe", "name": "game_mode", "samples": 0},
        expect="error",
        note="explicit [INTERNAL] reject; the max(1, samples) clamp in "
        "state_observer.py is dead code",
    ),
    Probe(
        "P1.ob.clear_unknown",
        tool="ppsspp_state_observer",
        args={"action": "clear", "name": "nope_xyz"},
        expect="ok",
        note="clear is idempotent-ok (differs from mem_remove's strict "
        "missing-target rejection — cross-tool inconsistency, "
        "documented in the verification report)",
    ),
    # ── session clamp ──
    Probe(
        "P1.se.wait_ready_zero",
        tool="ppsspp_session",
        args={"action": "wait_ready", "session_id": "__SESSION_ID__", "timeout_s": 0},
        note="timeout_s=0 clamps to 1.0 floor; live session → ready=true",
    ),
    # ── replay save/load roundtrip (harness never saves) ──
    Probe(
        "P1.rp.roundtrip",
        kind="composite",
        fn=_rp_roundtrip,
        note="begin→save→load→abort on the real wire",
    ),
    Probe(
        "P1.gr.double_record",
        kind="composite",
        fn=_gpu_double,
        note="two back-to-back gpu_record tickets",
    ),
    Probe(
        "P1.la.sections_all",
        kind="composite",
        fn=_la_sections_all,
        note="every address-book section must resolve",
    ),
    # ── final hygiene ──
    Probe(
        "P1.hygiene.final",
        kind="composite",
        fn=_hygiene_final,
        check=_check_hygiene,
        note="no bp/observer leaks, CPU still running",
    ),
]


# ── runner ───────────────────────────────────────────────────────────────


async def _boot(session: ClientSession, state: dict[str, Any]) -> dict[str, Any]:
    # R17 parity: a stale session holding the fixed PPSSPP port poisons
    # the start with PORT_CONFLICT (observed live with a dead pid entry).
    await _pre_clean(session, state)
    r = await call(
        session,
        "ppsspp_session",
        {"action": "start", "iso_path": ISO_PATH, "resilient": True},
        state,
    )
    if r["status"] != "ok":
        return {
            "status": "tool_error",
            "latency_ms": r["latency_ms"],
            "error": f"start failed: {r['error'][:200]}",
            "structured": None,
            "text": "",
        }
    sid = (r.get("structured") or {}).get("session_id")
    if not sid:
        return {
            "status": "tool_error",
            "latency_ms": r["latency_ms"],
            "error": "start returned no session_id",
            "structured": None,
            "text": "",
        }
    state["SESSION_ID"] = sid
    r_ready = await call(
        session,
        "ppsspp_session",
        {"action": "wait_ready", "session_id": sid, "timeout_s": 100.0},
        state,
    )
    if r_ready["status"] != "ok":
        return {
            "status": "tool_error",
            "latency_ms": r_ready["latency_ms"],
            "error": f"wait_ready failed: {r_ready['error'][:200]}",
            "structured": r_ready.get("structured"),
            "text": "",
        }
    await asyncio.sleep(12.0)  # title-screen settle (harness parity)
    return {
        "status": "ok",
        "latency_ms": round(r["latency_ms"] + r_ready["latency_ms"], 1),
        "error": "",
        "structured": r_ready.get("structured"),
        "text": f"booted {sid}",
    }


async def _run_probe(
    session: ClientSession, state: dict[str, Any], pr: Probe, phase: str
) -> dict[str, Any]:
    if pr.fn is not None:
        try:
            rec = await pr.fn(session, state)
        except Exception as e:  # noqa: BLE001
            rec = {
                "status": "exception",
                "error": f"{type(e).__name__}: {e}"[:300],
                "latency_ms": 0.0,
                "structured": None,
                "text": "",
            }
    elif pr.tool:
        rec = await call(session, pr.tool, dict(pr.args), state)
    else:  # pragma: no cover — misconfigured probe
        rec = {
            "status": "exception",
            "error": "probe misconfigured",
            "latency_ms": 0.0,
            "structured": None,
            "text": "",
        }
    problems = problems_for(rec, pr.expect)
    if rec["status"] == "ok" and pr.check is not None:
        s = rec.get("structured")
        if s is not None and not isinstance(s, dict):
            problems += [f"structured is {type(s).__name__}, not dict — cannot validate"]
        else:
            problems += pr.check(rec)
    return {
        "id": pr.id,
        "phase": phase,
        "status": rec["status"],
        "latency_ms": rec.get("latency_ms", 0.0),
        "error": rec.get("error", ""),
        "structured": rec.get("structured"),
        "text": rec.get("text", ""),
        "expect": pr.expect,
        "note": pr.note,
        "problems": problems,
    }


def _print(rec: dict[str, Any]) -> None:
    mark = "PASS" if passed(rec) else "FAIL"
    line = f"[{mark}] {rec['id']} ({rec['status']}, {rec['latency_ms']:.0f}ms)"
    if rec.get("error") and not passed(rec):
        line += f" — {str(rec['error'])[:140]}"
    if rec.get("problems"):
        line += f" | {rec['problems'][:2]}"
    print(line)
    sys.stdout.flush()


async def run(phase: str) -> dict[str, Any]:
    params = build_stdio_params(sys.executable)
    state: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"python": platform.python_version()}

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        t0 = time.perf_counter()
        init = await asyncio.wait_for(session.initialize(), timeout=60.0)
        meta["handshake_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        meta["protocol_version"] = getattr(init, "protocol_version", None) or getattr(
            init, "protocolVersion", None
        )

        if phase in ("all", "p0"):
            for pr in PROBES_P0:
                rec = await _run_probe(session, state, pr, "P0")
                records.append(rec)
                _print(rec)

        if phase in ("all", "p1"):
            boot = await _boot(session, state)
            boot_rec = {
                "id": "P1.session.boot",
                "phase": "P1",
                "status": boot["status"],
                "latency_ms": boot["latency_ms"],
                "error": boot["error"],
                "structured": boot["structured"],
                "text": boot["text"],
                "expect": "ok",
                "note": "resilient start + wait_ready + 12s settle",
                "problems": problems_for(boot, "ok"),
            }
            records.append(boot_rec)
            _print(boot_rec)
            if boot["status"] == "ok":
                for pr in PROBES_P1:
                    rec = await _run_probe(session, state, pr, "P1")
                    records.append(rec)
                    _print(rec)
                r_stop = await call(
                    session,
                    "ppsspp_session",
                    {"action": "stop", "session_id": state.get("SESSION_ID")},
                    state,
                )
                stop_rec = {
                    "id": "P1.session.stop",
                    "phase": "P1",
                    "status": r_stop["status"],
                    "latency_ms": r_stop["latency_ms"],
                    "error": r_stop["error"],
                    "structured": None,
                    "text": "",
                    "expect": "ok",
                    "note": "teardown",
                    "problems": problems_for(r_stop, "ok"),
                }
                records.append(stop_rec)
                _print(stop_rec)

    passed_n = sum(1 for r in records if passed(r))
    failed_n = len(records) - passed_n
    return {
        "meta": meta,
        "summary": {"passed": passed_n, "failed": failed_n, "total": len(records)},
        "records": records,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["all", "p0", "p1"], default="all")
    args = ap.parse_args()
    report = asyncio.run(run(args.phase))
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    s = report["summary"]
    print(
        f"\n=== boundary probe: {s['passed']} passed / {s['failed']} failed "
        f"(total {s['total']}) ==="
    )
    for r in report["records"]:
        if not passed(r):
            print(f"  FAIL {r['id']}: {r['status']} {str(r['error'])[:150]} {r['problems']}")
    print(f"report: {REPORT_PATH}")
    raise SystemExit(0 if s["failed"] == 0 else 1)


if __name__ == "__main__":  # pragma: no cover
    main()
