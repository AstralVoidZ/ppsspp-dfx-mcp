"""Real-MCP scenario verification harness for ppsspp-dfx-mcp.

Launches the MCP server as a subprocess using the EXACT same
command/args/cwd/env as the zcode project-level config
of the surrounding project), connects a real MCP client (mcp SDK,
stdio transport), and drives all 35 tools through a three-phase
scenario matrix:

- Phase A (no session): no-session guards, parameter boundaries,
  rate-limit burst.
- Phase B (live PPSSPP + game ISO): happy paths, boundaries,
  read/write round-trips.
- Phase C (teardown): session stop, post-stop guards, leftover checks.

Results are written to ``.ppsspp-dfx/output/real_mcp_verification/report.json``.

Usage:
    python -m ppsspp_dfx_mcp.scripts.verify_real_mcp [--phase all|a|b]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# R-E (2026-09-08): shared runner primitives — the MCP error
# classification, launch params and stale-session pre-clean live in
# _wire.py so this gate and probe_boundary_matrix.py cannot diverge.
# ── Server launch params — MUST mirror the project-level MCP client config ──
from _wire import (  # noqa: F401 — re-exported for scenario helpers
    ISO_PATH,  # noqa: F401 — env-driven (PPSSPP_DFX_TEST_ISO_PATH)
    PACKAGE_ROOT,
    WORKSPACE_ROOT,
    _is_mcp_error,
    build_stdio_params,
)
from _wire import (
    stop_all_sessions as _wire_stop_all_sessions,
)
from mcp import ClientSession
from mcp.client.stdio import stdio_client

PYTHON_EXE = sys.executable
SRC_DIR = str(PACKAGE_ROOT / "src")
PPSSPP_LOG = os.environ.get("PPSSPP_DFX_TEST_PPSSPP_LOG", "ppsspp_debug.log")
# S3 contract (2026-09-06): analyze_log is whitelisted to the .ppsspp-dfx
# tree — the analyze fixture lives inside output/ (the old external
# debug.log capture was removed with the August debug-script cleanup).
ANALYZE_FIXTURE_LOG = WORKSPACE_ROOT / ".ppsspp-dfx" / "output" / "analyze_fixture.log"


def _seed_analyze_fixture() -> None:
    """Write the analyze_log fixture (with ERROR lines to filter) into the
    whitelisted output tree, before the phase that exercises it."""
    ANALYZE_FIXTURE_LOG.parent.mkdir(parents=True, exist_ok=True)
    ANALYZE_FIXTURE_LOG.write_text(
        "INFO  game loaded ok\n"
        "ERROR HLE: bad syscall 0x1234\n"
        "WARNING: crc mismatch on block 7\n"
        "ERROR gpu: framebuffer timeout\n",
        encoding="utf-8",
    )


PPSSPP_MIRROR_LOG = WORKSPACE_ROOT / ".ppsspp-dfx" / "output" / "ppsspp.log"


def _seed_ppsspp_mirror() -> None:
    """Pre-seed the W4 mirror file so B.v3.analyze_default_mirror has a
    deterministic ERROR line to find. The server appends broadcast lines
    to the same file concurrently (append mode both sides — safe)."""
    PPSSPP_MIRROR_LOG.parent.mkdir(parents=True, exist_ok=True)
    with PPSSPP_MIRROR_LOG.open("a", encoding="utf-8") as f:
        f.write("ERROR harness-seed: mirror default-path probe\n")


REPORT_PATH = WORKSPACE_ROOT / ".ppsspp-dfx" / "output" / "real_mcp_verification" / "report.json"

SCRATCH = "0x09FE0000"  # high user-RAM scratch area (>=4KB clear of the top boundary)
GAME_MODE = "0x08A0D000"  # read-hot address — trace_memory_access traps here
CALL_TIMEOUT_S = 120.0

# ── O3/R11: per-tool latency budgets (ms), enforced on ok-records in
# phase B. Budgets are generous multiples of the v2-report p50 so
# environment jitter cannot flake them; a genuine regression (e.g. the
# S1-era 5s step path) exceeds them by orders of magnitude.
LATENCY_BUDGET_MS: dict[str, float] = {
    # H1: the tool's whole purpose is blocking up to timeout_s —
    # the plain timeout scenario waits 1.5s by design (the race
    # scenario returns before this gate applies).
    "ppsspp_wait_breakpoint": 5000,
    "ppsspp_step": 100,
    "ppsspp_search_disasm": 5000,
    "ppsspp_read_memory": 2000,
    "ppsspp_session": 10000,
    "ppsspp_screenshot": 1500,
    "ppsspp_dump_texture": 3000,
    "ppsspp_dump_clut": 3000,
    "ppsspp_state_observer": 2000,
    "ppsspp_smoke_test": 2000,
    "ppsspp_run_script": 2000,
    "ppsspp_gpu_stats": 1000,
    "ppsspp_gpu_record": 1500,
    "ppsspp_batch_step": 2000,
    "ppsspp_wait_frames": 2000,
    "ppsspp_analyze_log": 1000,
    # R-A (2026-09-08): budget covers the timeout-BY-DESIGN scenarios
    # (B.trace.timeout_clean waits 0.5s; B.wb.clamp_floor waits 0.5s).
    "ppsspp_trace_memory_access": 5000,
    "ppsspp_frame_snapshot": 1000,
    # R-F: dynamic exposed script tools (offline analysis reads files).
    "ppsspp_script_check_cpu_state": 2000,
    "ppsspp_script_find_0e_source": 5000,
}
LATENCY_BUDGET_DEFAULT_MS = 500.0


@dataclass
class Scenario:
    id: str
    tool: str
    args: dict[str, Any]
    expect: str = "ok"  # "ok" | "error" | "either"
    note: str = ""
    validator: Callable[[Any, dict[str, Any]], list[str]] | None = None
    # R19 (2026-09-06): frame-time-dominated scenarios (press tickets and
    # waits advance with GAME frames, not server work). Their latency
    # budget is baseline-p50 ×3 + 2s frame jitter instead of the fixed
    # per-tool dict budget — game-frame slowdowns (shader JIT,
    # title-screen processing) are environment, not regressions.
    frame_budget: bool = False


@dataclass
class Record:
    id: str
    tool: str
    phase: str
    expect: str
    note: str
    status: str = ""
    latency_ms: float = 0.0
    error: str = ""
    text_head: str = ""  # first 300 chars of text content
    structured: Any = None
    images: list = field(default_factory=list)
    problems: list = field(default_factory=list)

    @property
    def passed(self) -> bool:
        if self.expect == "either":
            return self.status not in ("timeout", "exception") and not self.problems
        if self.expect == "ok":
            return self.status == "ok" and not self.problems
        return self.status in ("tool_error", "rpc_error") and not self.problems


STATE: dict[str, Any] = {}
# Tools whose input schema declares a session_id property (built from
# list_tools at runtime — the client must pass session_id per call).
SESSION_ID_TOOLS: set[str] = set()


def subst(args: dict[str, Any]) -> dict[str, Any]:
    """Substitute __SESSION_ID__-style placeholders from STATE."""
    out = json.loads(json.dumps(args))  # deep copy

    def walk(v: Any) -> Any:
        if isinstance(v, str):
            for k, val in STATE.items():
                v = v.replace(f"__{k}__", str(val))
            return v
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        return v

    return walk(out)


def summarize_content(result: Any, rec: Record) -> None:
    texts: list[str] = []
    for c in getattr(result, "content", None) or []:
        ctype = getattr(c, "type", None)
        if ctype == "text":
            texts.append(c.text)
        elif ctype == "image":
            rec.images.append(
                [
                    getattr(c, "mimeType", "?"),
                    len(getattr(c, "data", "") or ""),
                ]
            )
    if texts:
        # 1200 chars is enough for error text and for the tools that still
        # answer in the text channel. Tool *metadata* must NOT be validated
        # from here any more: after the `tool-schema-contract` change every
        # tool publishes a contract and answers via `structured_content`
        # (the image tools have no text block at all). Validators read
        # `rec.structured` — see `_v_screenshot_meta`.
        rec.text_head = "\n".join(texts)[:1200]
    rec.structured = getattr(result, "structured_content", None)
    if rec.structured is None:
        rec.structured = getattr(result, "structuredContent", None)
    if rec.structured is not None:
        try:
            if len(json.dumps(rec.structured)) > 65536:
                rec.structured = {"_truncated": True, "note": "structured response >64KB"}
        except (TypeError, ValueError):            rec.structured = {"_unserializable": str(rec.structured)[:200]}


def _hex_of(v: Any) -> str:
    """Normalize register/memory values (hex-string or int) to a hex string."""
    if isinstance(v, int):
        return f"0x{v:X}"
    return str(v)


# ── Validators ──────────────────────────────────────────────────────────────


def _v_session_started(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    sid = s.get("session_id")
    if not sid:
        return ["no session_id in structured response"]
    state["SESSION_ID"] = sid
    if not s.get("pid"):
        return ["no pid in structured response"]
    state["PID"] = s["pid"]
    return []


def _v_image_present(rec: Record, state: dict[str, Any]) -> list[str]:
    if not rec.images:
        return ["no ImageContent block in response"]
    return []


def _v_memory_map(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    ranges = s.get("ranges") or s.get("regions") or []
    if not ranges:
        return ["no ranges in memory map response"]
    return []


def _v_scratch_value(expected_bytes: list[int]):
    """read_bytes returns value as a list of ints — compare byte lists."""

    def v(rec: Record, state: dict[str, Any]) -> list[str]:
        s = rec.structured or {}
        val = s.get("value", s.get("data"))
        if val != expected_bytes:
            return [f"scratch read-back {val!r} != expected {expected_bytes}"]
        return []

    return v


def _v_register_equal(expected: str):
    def v(rec: Record, state: dict[str, Any]) -> list[str]:
        s = rec.structured or {}
        val = _hex_of(s.get("value", "")).upper()
        tgt = _hex_of(expected).upper()
        if val != tgt:
            return [f"register read-back {val} != expected {tgt}"]
        return []

    return v


def _v_bp_in_list(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    bps = s.get("breakpoints", [])
    if not any(int(b.get("address", 0)) == int(SCRATCH, 16) for b in bps if isinstance(b, dict)):
        return ["scratch breakpoint not present in listing"]
    return []


def _v_pc_advanced(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    val = _hex_of(s.get("pc", s.get("value", ""))).upper()
    state["PC_AFTER_STEP"] = val
    before = state.get("PC_BEFORE_STEP", "").upper()
    if not before:
        return ["PC_BEFORE_STEP was not captured"]
    if val == before:
        return [f"pc did not advance after step into ({val})"]
    return []


def _v_capture_pc(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    val = _hex_of(s.get("pc", s.get("value", "")))
    state["PC"] = val
    state["PC_BEFORE_STEP"] = val
    if not val.startswith("0x"):
        return [f"pc value not hex-shaped: {val!r}"]
    return []


def _v_capture_a1(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    state["R3_ORIG"] = _hex_of(s.get("value", "")) or "0x0"
    return []


def _v_read_string_abc(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    val = str(s.get("value", ""))
    if "ABC" not in val:
        return [f"read_string value {val!r} does not contain 'ABC'"]
    return []


def _v_burst_limited(rec: Record, state: dict[str, Any]) -> list[str]:
    if not rec.structured:
        return ["burst record missing structured summary"]
    n_err = rec.structured.get("rate_limited", -1)
    if n_err <= 0:
        return [f"expected >0 RATE_LIMIT_EXCEEDED responses, got {n_err}"]
    return []


def _v_batch_all_ok(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    if s.get("failed", 0) or s.get("skipped", 0):
        return [
            f"batch reported failed={s.get('failed')} skipped={s.get('skipped')} "
            f"while tool returned ok (hollow success)"
        ]
    return []


def _v_game_mode(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    if "value" not in s and "data" not in s:
        return ["no value field in read response"]
    state["GAME_MODE"] = _hex_of(s.get("value", s.get("data")))
    return []


# ── V2 validators (post-review-fix contracts) ──────────────────────────────

_LONG_STR_HEX = "41" * 8000 + "00"  # 8000 'A's + NUL — proves >4096 reads


def _v_v2_string_8000(rec: Record, state: dict[str, Any]) -> list[str]:
    """W1 proof: read_string(max_len=65536) returns the full 8000-byte
    string — the old client-side 4096 re-clamp would truncate it."""
    s = rec.structured or {}
    val = s.get("value", "")
    if not isinstance(val, str) or len(val) != 8000:
        return [
            f"expected 8000-char string (W1 64KiB contract), got "
            f"{len(val) if isinstance(val, str) else type(val).__name__}"
        ]
    if s.get("truncated") is True or "TRUNCATED" in str(s.get("text", "")):
        return ["string reported truncated — cap must not trigger at 8000"]
    return []


# ── V3 validators (review-r2 behavioral contracts) ─────────────────────────


def _v_scan_pattern_cap(rec: Record, state: dict[str, Any]) -> list[str]:
    """S1: the rejection must name the scan pattern cap (fail-fast with
    the budget named, not a generic internal error)."""
    hay = rec.error + rec.text_head
    if "[INTERNAL]" not in hay or "scan cap" not in hay:
        return ["expected '[INTERNAL] … scan cap' rejection (R15 code prefix + S1)"]
    return []


def _v_mem_remove_missing(rec: Record, state: dict[str, Any]) -> list[str]:
    """S3: explicit-size mem_remove against nothing must fail loudly."""
    hay = rec.error + rec.text_head
    if "[BREAKPOINT_ERROR]" not in hay or "no memory breakpoint" not in hay:
        return ["expected '[BREAKPOINT_ERROR] no memory breakpoint' (R15 + S3)"]
    return []


def _v_analyze_default_mirror(rec: Record, state: dict[str, Any]) -> list[str]:
    """W4: default path (log_path=None) must read the mirrored ppsspp log
    (the pre-seeded ERROR line must appear)."""
    s = rec.structured or {}
    source = str(s.get("log_path", ""))
    if not source.endswith("ppsspp.log"):
        return [f"default source should be the mirror file, got {source!r}"]
    if int(s.get("count", -1)) < 1:
        return [f"pre-seeded ERROR line not found (count={s.get('count')})"]
    return []


def _v_screenshot_meta(rec: Record, state: dict[str, Any]) -> list[str]:
    """S2: screenshot metadata must carry the explicit 'empty' flag.

    `ppsspp_screenshot` was rewritten (openspec `imagecontent-output`) to
    return `content=[ImageContent]` + `structured_content=<metadata>` — the
    metadata JSON text block is gone, so it must be read from
    `rec.structured`. Reading `text_head` here kept reporting "metadata JSON
    not parseable" and the scenario could never pass again.
    """
    if not rec.images:
        return ["no ImageContent block in response"]
    meta = rec.structured
    if not isinstance(meta, dict) or not meta:
        return [f"no structured_content metadata: {meta!r}"]
    if "empty" not in meta:
        return [f"metadata missing explicit 'empty' flag (S2): {sorted(meta.keys())}"]
    return []


def _v_batch_compact_failure(rec: Record, state: dict[str, Any]) -> list[str]:
    """S6: BATCH_STEP_FAILED message must carry the compact per-step
    summary and NOT the old full-JSON 'details=' envelope."""
    err = rec.error or rec.text_head
    # R15: the code now renders as a "[BATCH_STEP_FAILED]" prefix in the
    # wire text — assert it plus the compact summary shape.
    if "[BATCH_STEP_FAILED]" not in err:
        return ["expected '[BATCH_STEP_FAILED]' code prefix (R15)"]
    if "executed" not in err or "failed" not in err:
        return ["expected the executed/failed summary line in the error"]
    if "step[0]" not in err:
        return ["expected compact 'step[0] type: error' summary (S6)"]
    if "details=" in err:
        return ["S6 violated: full JSON 'details=' envelope is back"]
    return []


def _v_session_busy(rec: Record, state: dict[str, Any]) -> list[str]:
    """W1: a call queued behind a long holder must fail with SESSION_BUSY
    (explicit error, not a silent infinite queue) while the holder itself
    completes normally."""
    s = rec.structured or {}
    hay = " ".join([str(s.get("victim_text", "")), rec.error, rec.text_head])
    if "[SESSION_BUSY]" not in hay:
        return [
            f"expected '[SESSION_BUSY]' error (W1+R15), got victim_text="
            f"{str(s.get('victim_text'))[:150]!r}"
        ]
    if not s.get("holder_ok", False):
        return ["holder call failed — serialization must not break the long-running call itself"]
    return []


def _v_wait_bp_timeout(rec: Record, state: dict[str, Any]) -> list[str]:
    """H1: wait_breakpoint with NO breakpoint armed must return a
    hit=false RESULT (not an error) after the short budget."""
    s = rec.structured or {}
    if s.get("hit") is not False:
        return [f"expected hit=false (no bp armed), got hit={s.get('hit')!r}"]
    if s.get("already_paused"):
        return ["already_paused should be false on a running game"]
    return []


def _v_wait_bp_no_lock_race(rec: Record, state: dict[str, Any]) -> list[str]:
    """H1 A-H1-2: a quick read issued DURING a breakpoint wait must
    succeed immediately — the wait is lock-free (PARTIAL_HOLD)."""
    s = rec.structured or {}
    if s.get("victim_status") != "ok":
        return [f"victim read failed during wait: {str(s.get('victim_text'))[:150]!r}"]
    if "SESSION_BUSY" in str(s.get("victim_text", "")):
        return [
            "victim got SESSION_BUSY — wait_breakpoint is holding "
            "the session lock (contract violation)"
        ]
    if not s.get("holder_ok", False):
        return ["holder wait_breakpoint failed"]
    if rec.latency_ms > 3000.0:
        return [
            f"victim latency {rec.latency_ms:.0f}ms ≈ holder duration "
            f"— the read queued instead of running concurrently"
        ]
    return []


# ── R-A validators (probe-collected contracts, 2026-09-08) ─────────────────


def _v_trace_hit(rec: Record, state: dict[str, Any]) -> list[str]:
    """trace_memory_access hit contract: four-key result + S3 attribution."""
    s = rec.structured or {}
    p: list[str] = []
    if s.get("hit") is not True:
        p.append(f"hit={s.get('hit')!r}")
    if s.get("bp_removed") is not True:
        p.append(f"bp_removed={s.get('bp_removed')!r}")
    if s.get("resumed") is not True:
        p.append(f"resumed={s.get('resumed')!r}")
    hits = s.get("hits") or []
    if not hits:
        p.append("hits empty")
    else:
        h = hits[0]
        if not str(h.get("pc", "")).startswith("0x"):
            p.append(f"hit pc not hex-shaped: {h.get('pc')!r}")
        if int(h.get("mem_hits", 0)) < 1:
            p.append(f"mem_hits={h.get('mem_hits')!r} (S3 attribution absent)")
    return p


def _v_trace_hit_full(rec: Record, state: dict[str, Any]) -> list[str]:
    p = _v_trace_hit(rec, state)
    hits = (rec.structured or {}).get("hits") or []
    if hits:
        if "registers" not in hits[0]:
            p.append("want_registers=true but no registers key")
        if "backtrace" not in hits[0]:
            p.append("want_backtrace=true but no backtrace key")
    return p


def _v_trace_timeout(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    p: list[str] = []
    if s.get("hit") is not False:
        p.append(f"hit={s.get('hit')!r} (never-hit address must not hit)")
    if s.get("bp_removed") is not True:
        p.append(f"bp_removed={s.get('bp_removed')!r} (timeout must clean up)")
    return p


def _v_trace_paused(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    p: list[str] = []
    if s.get("already_paused") is not True:
        p.append(f"already_paused={s.get('already_paused')!r}")
    if s.get("hit") is not False:
        p.append(f"hit={s.get('hit')!r} (paused CPU cannot hit)")
    return p


def _v_wb_paused(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    p: list[str] = []
    if s.get("hit") is not True:
        p.append(f"hit={s.get('hit')!r} (already-paused must short-circuit)")
    if s.get("already_paused") is not True:
        p.append(f"already_paused={s.get('already_paused')!r}")
    if not str(s.get("pc", "")).startswith("0x"):
        p.append(f"pc not hex-shaped: {s.get('pc')!r}")
    return p


def _v_wb_clamp_floor(rec: Record, state: dict[str, Any]) -> list[str]:
    """timeout_s=0.1 must clamp to the 0.5s floor — observable latency."""
    if rec.latency_ms < 450.0:
        return [
            f"latency {rec.latency_ms:.0f}ms < 450ms — timeout_s=0.1 "
            f"was NOT clamped to the 0.5 floor"
        ]
    return []


def _v_fs_running(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    regs = s.get("registers")
    p: list[str] = []
    if s.get("was_stepping") is not False:
        p.append(f"was_stepping={s.get('was_stepping')!r}")
    if s.get("resumed") is not True:
        p.append(f"resumed={s.get('resumed')!r} (running CPU must be resumed)")
    if not str(s.get("pc", "")).startswith("0x"):
        p.append(f"pc not hex-shaped: {s.get('pc')!r}")
    if not (isinstance(regs, dict) and regs):
        p.append(f"registers={type(regs).__name__} (expected non-empty dict)")
    return p


def _v_fs_stays_paused(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    p: list[str] = []
    if s.get("was_stepping") is not True:
        p.append(f"was_stepping={s.get('was_stepping')!r}")
    if s.get("resumed") is not False:
        p.append(f"resumed={s.get('resumed')!r} (already-paused CPU must STAY paused)")
    return p


def _v_fs_no_regs(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    if s.get("registers") not in (None, {}, []):
        return [f"registers populated despite want_registers=false: {s.get('registers')!r}"]
    return []


def _v_fs_probes(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    probes = s.get("probes") or {}
    if not probes:
        return ["snapshot carried no probes section"]
    if "harness_scratch" not in json.dumps(probes):
        return [f"harness_scratch missing from probes section: {json.dumps(probes)[:200]}"]
    return []


def _v_smoke_liveness(rec: Record, state: dict[str, Any]) -> list[str]:
    """R-C three-check liveness judge (F-07): game_mode_valid is
    game-phase dependent (attract mode) and must NOT gate liveness."""
    s = rec.structured or {}
    checks = {c.get("name"): c.get("passed") for c in s.get("checks", []) if isinstance(c, dict)}
    missing = [k for k in ("iso_loaded", "cpu_running", "ws_connected") if k not in checks]
    if missing:
        return [f"smoke battery missing liveness checks: {missing}"]
    failed = [k for k in ("iso_loaded", "cpu_running", "ws_connected") if checks[k] is not True]
    return [f"liveness checks failed: {failed}"] if failed else []


def _v_bp_mem_list_empty(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    bps = s.get("breakpoints", [])
    if bps:
        addrs = [hex(int(b.get("address", 0))) for b in bps if isinstance(b, dict)]
        return [f"leaked mem breakpoints after full phase B: {addrs}"]
    return []


def _v_observer_no_scratch(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    flat = json.dumps(s)
    if "harness_scratch" in flat:
        return ["observer probe harness_scratch leaked (clear must have removed it before hygiene)"]
    return []


def _v_read_cap_exact(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    if s.get("_truncated"):
        # summarize_content mirrors only the first 64KB — a truncated
        # mirror of a 65536-byte read IS the success evidence.
        return []
    val = s.get("value") or []
    if len(val) != 65536:
        return [f"read_bytes(65536) returned {len(val)} bytes — W1 exact cap boundary violated"]
    return []


# ── R-B generic shape validators (anti hollow-ok, batch 1) ─────────────────


def _v_has_keys(*keys: str):
    def v(rec: Record, state: dict[str, Any]) -> list[str]:
        s = rec.structured or {}
        missing = [k for k in keys if k not in s]
        return [f"response missing shape keys {missing}"] if missing else []

    return v


def _v_value_hex(rec: Record, state: dict[str, Any]) -> list[str]:
    """value-ish field (value/data/pc) must be hex-shaped or an int."""
    s = rec.structured or {}
    val = s.get("value", s.get("data", s.get("pc")))
    txt = _hex_of(val) if val is not None else ""
    if not txt.upper().startswith("0X") and not isinstance(val, int):
        return [f"value not numeric/hex-shaped: {val!r}"]
    return []


def _v_reg_query(rec: Record, state: dict[str, Any]) -> list[str]:
    """query(action=register) carries the RAW cpu.getReg payload under
    data — uintValue is the int register value (not a hex string)."""
    s = rec.structured or {}
    if s.get("_truncated"):
        return []
    data = s.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("uintValue"), int):
        return [f"register query data not a cpu.getReg payload: {data!r}"]
    return []


def _v_data_present(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    if s.get("_truncated"):
        return []  # >64KB mirror — the payload obviously exists
    if s.get("data") is None:
        return ["query response carried data=None (hollow payload)"]
    return []


def _v_script_output(rec: Record, state: dict[str, Any]) -> list[str]:
    """ScriptRunOutput contract: {name, output, output_model} with a
    non-empty output payload. F-10 (2026-09-08): DYNAMICALLY registered
    ppsspp_script_* tools carry this JSON in the TEXT channel only
    (structuredContent is null on this SDK) — accept either channel."""
    s = rec.structured or {}
    if "output" in s:
        if not isinstance(s.get("output"), dict) or not s["output"]:
            return [f"script output hollow: {s.get('output')!r}"]
        return []
    hay = rec.text_head
    if '"name"' in hay and '"output"' in hay:
        return []
    return [
        "script response carried neither structured payload nor "
        f"text-channel ScriptRunOutput JSON: {hay[:120]!r}"
    ]


# prompts / resources validators (session-independent surface)


def _v_prompts_list(rec: Record, state: dict[str, Any]) -> list[str]:
    names = (rec.structured or {}).get("prompts", [])
    missing = [n for n in ("memory-trace-wizard", "memory-breakpoint-wizard") if n not in names]
    return [f"prompts missing: {missing}"] if missing else []


def _v_prompt_render(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    if s.get("n_messages", 0) < 1:
        return ["prompt rendered no messages"]
    if "trace_memory_access" not in rec.text_head:
        return ["prompt text does not route to ppsspp_trace_memory_access"]
    return []


def _v_resources_list(rec: Record, state: dict[str, Any]) -> list[str]:
    uris = (rec.structured or {}).get("resources", [])
    missing = [u for u in ("ppsspp://game-state", "ppsspp://registers") if u not in uris]
    return [f"resources missing: {missing}"] if missing else []


def _v_exposed_scripts(rec: Record, state: dict[str, Any]) -> list[str]:
    s = rec.structured or {}
    scripts = s.get("scripts", [])
    exposed = {sc.get("name") for sc in scripts if isinstance(sc, dict) and sc.get("exposed")}
    missing = [
        n for n in ("hello_diagnostic", "find_0e_source", "check_cpu_state") if n not in exposed
    ]
    return [f"exposed scripts missing: {missing}"] if missing else []


def _v_race_contract(rec: Record, state: dict[str, Any]) -> list[str]:
    """Race scenarios carry a structured verdict; a bare ok proves nothing."""
    s = rec.structured or {}
    if not s.get("holder_ok", False):
        return ["holder call failed"]
    if not s.get("victim_status"):
        return ["race verdict missing victim_status"]
    return []


def _v_bp_size_guard(rec: Record, state: dict[str, Any]) -> list[str]:
    """F-01 wire lock: the rejection must name the invalid size."""
    hay = rec.error + rec.text_head
    if "[INTERNAL]" not in hay or "invalid memcheck size" not in hay:
        return ["expected '[INTERNAL] invalid memcheck size …' (F-01)"]
    return []


def _v_mem_list_size(expected: int):
    def v(rec: Record, state: dict[str, Any]) -> list[str]:
        s = rec.structured or {}
        for b in s.get("breakpoints", []):
            if isinstance(b, dict) and int(b.get("address", 0)) == int(SCRATCH, 16):
                if int(b.get("size", 0)) != expected:
                    return [f"SCRATCH memcheck size {b.get('size')} != {expected}"]
                return []
        return [f"no memcheck at {SCRATCH} (size={expected} arm lost?)"]

    return v


def _v_replay_empty_rejected(rec: Record, state: dict[str, Any]) -> list[str]:
    """F-02 wire lock: empty capture rejected with the code prefix."""
    hay = rec.error + rec.text_head
    if "[REPLAY_EMPTY]" not in hay or "no frames" not in hay:
        return ["expected '[REPLAY_EMPTY] no frames captured …' (F-02)"]
    return []


VALIDATORS: dict[str, Callable] = {
    "B.session.start": _v_session_started,
    "B.read.u32.game_mode": _v_game_mode,
    "B.memory_map": _v_memory_map,
    "B.image.screenshot": _v_image_present,
    "B.image.dump_texture": _v_image_present,
    "B.scratch.read_u8": _v_scratch_value([0xAA]),
    "B.scratch.read_u16": _v_scratch_value([0xEF, 0xBE]),
    "B.scratch.read_u32": _v_scratch_value([0xEF, 0xBE, 0xAD, 0xDE]),
    "B.scratch.read_bytes4": _v_scratch_value([1, 2, 3, 4]),
    "B.assemble.readback": _v_scratch_value([0, 0, 0, 0]),
    "B.reg.a1_orig": _v_capture_a1,
    "B.read.string": _v_read_string_abc,
    "B.get_pc": _v_capture_pc,
    "B.bp.cpu_list": _v_bp_in_list,
    "B.bp.mem_list": _v_bp_in_list,
    "B.batch_step.mixed": _v_batch_all_ok,
    "A.burst.list_addresses": _v_burst_limited,
    # ── V2 additions: post-review-fix contracts ──
    # ── V3 additions: review-r2 behavioral contracts ──
    "B.v3.scan_pattern_over_cap": _v_scan_pattern_cap,
    "B.v3.mem_remove_explicit_size_missing": _v_mem_remove_missing,
    "B.v3.analyze_default_mirror": _v_analyze_default_mirror,
    "B.v3.screenshot_empty_flag": _v_screenshot_meta,
    "B.v3.batch_step_failure_summary": _v_batch_compact_failure,
    "B.v3.session_busy_victim": _v_session_busy,
}


# ── Phase A: no-session guards + boundaries + rate limit ───────────────────

SESSION_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("ppsspp_read_memory", {"action": "read_bytes", "address": "0x08804000", "size": 4}),
    ("ppsspp_write_memory", {"address": SCRATCH, "data": "0xAA", "format": "u8"}),
    ("ppsspp_memory_map", {}),
    ("ppsspp_get_pc", {}),
    ("ppsspp_query", {"action": "registers"}),
    ("ppsspp_write_register", {"name": "r3", "value": "0x0"}),
    ("ppsspp_evaluate", {"expression": "pc"}),
    ("ppsspp_disassemble", {"address": "0x08804000", "count": 4}),
    ("ppsspp_search_disasm", {"address": "0x08804000", "match": "jr"}),
    ("ppsspp_assemble", {"address": SCRATCH, "code": "nop"}),
    ("ppsspp_breakpoint", {"action": "list"}),
    ("ppsspp_step", {"action": "pause"}),
    ("ppsspp_state_observer", {"action": "list"}),
    ("ppsspp_batch_step", {"steps": [{"type": "wait", "frames": 1}]}),
    ("ppsspp_wait_frames", {"frames": 1}),
    ("ppsspp_press_button", {"button": "cross", "duration": 1}),
    ("ppsspp_hold_buttons", {"buttons": "cross"}),
    ("ppsspp_send_analog", {"x": 128, "y": 128}),
    ("ppsspp_screenshot", {}),
    ("ppsspp_dump_texture", {}),
    ("ppsspp_dump_clut", {}),
    ("ppsspp_gpu_stats", {}),
    ("ppsspp_gpu_record", {}),
    ("ppsspp_replay", {"action": "status"}),
    ("ppsspp_smoke_test", {}),
    ("ppsspp_memory_info_search", {"match": "Game"}),
    # R-A: H1/H2 tools join the no-session guard sweep (they were added
    # after the sweep was written — v4 report gap G-1's guard half).
    ("ppsspp_wait_breakpoint", {"timeout_s": 0.5}),
    ("ppsspp_trace_memory_access", {"address": GAME_MODE}),
    ("ppsspp_frame_snapshot", {}),
]

PHASE_A: list[Scenario] = [
    Scenario("A.health.ok", "ppsspp_health", {}),
    Scenario("A.session_list.empty", "ppsspp_session_list", {}),
    Scenario("A.session.get.no_id", "ppsspp_session", {"action": "get"}, "error"),
    Scenario("A.session.stop.no_id", "ppsspp_session", {"action": "stop"}, "error"),
    Scenario("A.session.start.no_iso", "ppsspp_session", {"action": "start"}, "error"),
    Scenario(
        "A.session.start.bad_iso",
        "ppsspp_session",
        {"action": "start", "iso_path": "Z:/definitely/not/real.iso"},
        "error",
    ),
    Scenario(
        "A.convert.ida_to_ppsspp.zero",
        "ppsspp_convert_address",
        {"address": "0x0", "mode": "ida_to_ppsspp"},
    ),
    Scenario(
        "A.convert.ppsspp_to_ida.base",
        "ppsspp_convert_address",
        {"address": "0x08804000", "mode": "ppsspp_to_ida"},
    ),
    Scenario(
        "A.convert.bad_hex", "ppsspp_convert_address", {"address": "zzz", "mode": "auto"}, "error"
    ),
    Scenario(
        "A.convert.below_base",
        "ppsspp_convert_address",
        {"address": "0x08000000", "mode": "ppsspp_to_ida"},
        "error",
    ),
    Scenario("A.list_addresses.default", "ppsspp_list_addresses", {}),
    Scenario(
        "A.list_addresses.bad_section",
        "ppsspp_list_addresses",
        {"section": "no_such_section"},
        "error",
    ),
    Scenario("A.list_scripts", "ppsspp_list_scripts", {}),
    Scenario("A.reload_scripts", "ppsspp_reload_scripts", {}),
    Scenario(
        "A.run_script.unknown", "ppsspp_run_script", {"name": "definitely_not_a_script"}, "error"
    ),
    Scenario(
        "A.analyze_log.missing", "ppsspp_analyze_log", {"log_path": "Z:/no/such/log.log"}, "error"
    ),
    Scenario(
        "A.read_memory.unknown_action",
        "ppsspp_read_memory",
        {"action": "read_u64", "address": "0x08804000"},
        "error",
    ),
    Scenario("A.read_memory.no_address", "ppsspp_read_memory", {"action": "read_bytes"}, "error"),
    Scenario(
        "A.read_memory.bad_hex",
        "ppsspp_read_memory",
        {"action": "read_bytes", "address": "kernel", "size": 4},
        "error",
    ),
    Scenario("A.convert_address.no_args", "ppsspp_convert_address", {}, "error"),
    # ── R-A additions: MCP surface beyond call_tool (v4 report G-4) ──
    # Prompts + resources are session-independent; they must be exercised
    # on the REAL wire, not only via the offline mcp_inspector stubs.
    # Kinds __prompts_list__/__prompt_get__/__resources_list__/
    # __resource_read__ are handled in _call (pseudo tools keep the
    # latency grouping readable).
    Scenario(
        "A.prompts.list",
        "(prompts)",
        {"__prompts_list__": True},
        "ok",
        validator=_v_prompts_list,
        note="R-A: kebab-case wizard prompts discoverable on the wire",
    ),
    Scenario(
        "A.prompts.trace_wizard_renders",
        "(prompt)",
        {"__prompt_get__": "memory-trace-wizard", "__prompt_args__": {"address": GAME_MODE}},
        "ok",
        validator=_v_prompt_render,
        note="R-A: prompt content must route to the H1 trace tool",
    ),
    Scenario(
        "A.prompts.unknown",
        "(prompt)",
        {"__prompt_get__": "no_such_wizard", "__prompt_args__": None},
        "error",
        note="unknown prompt → deterministic rejection",
    ),
    Scenario(
        "A.resources.list",
        "(resources)",
        {"__resources_list__": True},
        "ok",
        validator=_v_resources_list,
    ),
    Scenario(
        "A.resources.guard_no_session",
        "(resource)",
        {"__resource_read__": "ppsspp://game-state"},
        "error",
        note="single-session guard: zero sessions → deterministic "
        "rejection (deterministic because run() pre-cleans "
        "stale sessions before phase A — F-06)",
    ),
]
# No-session guard sweep for every session-dependent tool.
PHASE_A += [
    Scenario(
        f"A.guard.{tool}",
        tool,
        args,
        "ok" if tool == "ppsspp_state_observer" else "error",
        note="no-session guard"
        + (
            " (list action is session-independent by design)"
            if tool == "ppsspp_state_observer"
            else ""
        ),
    )
    for tool, args in SESSION_TOOLS
]
PHASE_A.append(
    Scenario(
        "A.burst.list_addresses",
        "ppsspp_list_addresses",
        {"__burst__": 70},
        "ok",
        note="rate-limit burst probe",
    )
)


# ── Phase B: live session matrix ────────────────────────────────────────────

PHASE_B: list[Scenario] = [
    Scenario(
        "B.session.start",
        "ppsspp_session",
        {"action": "start", "iso_path": ISO_PATH, "resilient": True},
        note="H2: resilient start exercised on the real wire each run "
        "(recovered>=0; wedge heal is server-side now)",
    ),
    Scenario(
        "B.wait_breakpoint.timeout",
        "ppsspp_wait_breakpoint",
        {"timeout_s": 1.5},
        "ok",
        validator=_v_wait_bp_timeout,
        note="H1: no bp armed → hit=false result (not an error)",
    ),
    Scenario(
        "B.wait_breakpoint.no_lock_race",
        "ppsspp_wait_breakpoint",
        {
            "timeout_s": 4.0,
            "__race__": {
                "holder": {
                    "tool": "ppsspp_wait_breakpoint",
                    "args": {"session_id": "__SESSION_ID__", "timeout_s": 4.0},
                },
                "victim": {
                    "tool": "ppsspp_read_memory",
                    "args": {
                        "session_id": "__SESSION_ID__",
                        "action": "read_u32",
                        "address": "0x08804000",
                    },
                },
                "delay_s": 0.5,
                "victim_expect_error": False,
            },
        },
        "ok",
        validator=_v_wait_bp_no_lock_race,
        note="H1 A-H1-2: quick read during a 4s breakpoint wait "
        "succeeds (lock-free wait, no SESSION_BUSY)",
    ),
    # ── R-A: wait_breakpoint / trace_memory_access contracts collected
    # by probe_boundary_matrix.py (v4 report G-1) ──
    Scenario(
        "B.wb.clamp_floor",
        "ppsspp_wait_breakpoint",
        {"timeout_s": 0.1},
        "ok",
        validator=_v_wb_clamp_floor,
        note="timeout_s=0.1 clamps to the 0.5s floor — latency must reflect the clamped budget",
    ),
    Scenario(
        "B.wb.pause_for",
        "ppsspp_step",
        {"action": "pause"},
        "ok",
        note="R-A setup: pause for the already-paused short-circuit",
    ),
    Scenario(
        "B.wb.paused_short_circuit",
        "ppsspp_wait_breakpoint",
        {"timeout_s": 2.0},
        "ok",
        validator=_v_wb_paused,
        note="wait on a paused CPU short-circuits hit=true/already_paused=true",
    ),
    Scenario(
        "B.wb.resume_after",
        "ppsspp_step",
        {"action": "resume"},
        "ok",
        note="R-A teardown: restore running state",
    ),
    Scenario(
        "B.trace.hit",
        "ppsspp_trace_memory_access",
        {"address": GAME_MODE, "access": "read", "size": 4, "timeout_s": 20.0},
        "ok",
        validator=_v_trace_hit,
        note="read-hot trap: hit + S3 mem_hits + bp cleanup + resume",
    ),
    Scenario(
        "B.trace.hit_full",
        "ppsspp_trace_memory_access",
        {
            "address": GAME_MODE,
            "access": "read",
            "size": 4,
            "timeout_s": 20.0,
            "want_registers": True,
            "want_backtrace": True,
        },
        "ok",
        validator=_v_trace_hit_full,
        note="capture options: registers + backtrace present in hit",
    ),
    Scenario(
        "B.trace.timeout_clean",
        "ppsspp_trace_memory_access",
        {"address": "0x09FF8000", "access": "read", "size": 4, "timeout_s": 0.5},
        "ok",
        validator=_v_trace_timeout,
        note="never-hit address: hit=false + bp_removed=true",
    ),
    Scenario(
        "B.trace.pause_for",
        "ppsspp_step",
        {"action": "pause"},
        "ok",
        note="R-A setup: pause for the trace short-circuit",
    ),
    Scenario(
        "B.trace.paused_short_circuit",
        "ppsspp_trace_memory_access",
        {"address": GAME_MODE, "access": "read", "size": 4, "timeout_s": 2.0},
        "ok",
        validator=_v_trace_paused,
        note="paused CPU: no arm, hit=false, already_paused=true",
    ),
    Scenario(
        "B.trace.resume_after",
        "ppsspp_step",
        {"action": "resume"},
        "ok",
        note="R-A teardown: restore running state",
    ),
    Scenario(
        "B.read.u32.game_mode",
        "ppsspp_read_memory",
        {"action": "read_u32", "address": "0x08A0D000"},
    ),
    Scenario(
        "B.read.bytes.code",
        "ppsspp_read_memory",
        {"action": "read_bytes", "address": "0x08804000", "size": 16},
    ),
    Scenario(
        "B.write.string_seed",
        "ppsspp_write_memory",
        {"address": "0x09FE0020", "data": "41424300", "format": "bytes"},
    ),
    Scenario(
        "B.read.string",
        "ppsspp_read_memory",
        {"action": "read_string", "address": "0x09FE0020"},
        note="read known seeded string (ABC\\0)",
    ),
    Scenario(
        "B.read.scan_prologue",
        "ppsspp_read_memory",
        {
            "action": "scan",
            "pattern": "E0FFBD27",
            "start_addr": "0x08804000",
            "end_addr": "0x08850000",
            "max_results": 3,
        },
    ),
    Scenario(
        "B.read.huge_size",
        "ppsspp_read_memory",
        {"action": "read_bytes", "address": "0x08804000", "size": 1048576},
        "error",
        note="R12 定版: 1MiB > 64KiB 单读上限（F-6/W1 常量），确定性拒绝",
    ),
    Scenario(
        "B.read.cap_exact_65536",
        "ppsspp_read_memory",
        {"action": "read_bytes", "address": "0x08804000", "size": 65536},
        "ok",
        validator=_v_read_cap_exact,
        note="R-A: 65536 is the ACCEPT boundary of the W1 cap "
        "(harness previously only probed 1MiB)",
    ),
    Scenario(
        "B.read.over_cap_65537",
        "ppsspp_read_memory",
        {"action": "read_bytes", "address": "0x08804000", "size": 65537},
        "error",
        note="R-A: 65537 is the first rejected size",
    ),
    Scenario("B.memory_map", "ppsspp_memory_map", {}, validator=_v_memory_map),
    Scenario(
        "B.write.u8", "ppsspp_write_memory", {"address": SCRATCH, "data": "0xAA", "format": "u8"}
    ),
    Scenario(
        "B.scratch.read_u8",
        "ppsspp_read_memory",
        {"action": "read_bytes", "address": SCRATCH, "size": 1},
    ),
    Scenario(
        "B.write.u16",
        "ppsspp_write_memory",
        {"address": "0x09FE0002", "data": "0xBEEF", "format": "u16"},
    ),
    Scenario(
        "B.scratch.read_u16",
        "ppsspp_read_memory",
        {"action": "read_bytes", "address": "0x09FE0002", "size": 2},
    ),
    Scenario(
        "B.write.u32",
        "ppsspp_write_memory",
        {"address": "0x09FE0004", "data": "0xDEADBEEF", "format": "u32"},
    ),
    Scenario(
        "B.scratch.read_u32",
        "ppsspp_read_memory",
        {"action": "read_bytes", "address": "0x09FE0004", "size": 4},
    ),
    Scenario(
        "B.write.bytes",
        "ppsspp_write_memory",
        {"address": "0x09FE0010", "data": "01020304", "format": "bytes"},
    ),
    Scenario(
        "B.scratch.read_bytes4",
        "ppsspp_read_memory",
        {"action": "read_bytes", "address": "0x09FE0010", "size": 4},
    ),
    Scenario(
        "B.write.u8.overflow",
        "ppsspp_write_memory",
        {"address": SCRATCH, "data": "0x100", "format": "u8"},
        "error",
    ),
    Scenario(
        "B.write.bytes.bad_hex",
        "ppsspp_write_memory",
        {"address": SCRATCH, "data": "GG", "format": "bytes"},
        "error",
    ),
    Scenario(
        "B.write.force_protected",
        "ppsspp_write_memory",
        {"address": "0x08804000", "data": "0x00", "format": "u32"},
        "error",
        note="protected code segment without force=True",
    ),
    Scenario("B.query.game_state", "ppsspp_query", {"action": "game_state"}),
    Scenario(
        "B.query.registers", "ppsspp_query", {"action": "registers"}, note="capture r3 original"
    ),
    Scenario(
        "B.query.register.pc",
        "ppsspp_query",
        {"action": "register", "name": "pc"},
        note="capture pc",
    ),
    Scenario("B.query.register.a0", "ppsspp_query", {"action": "register", "name": "a0"}),
    Scenario(
        "B.query.register.bad_name", "ppsspp_query", {"action": "register", "name": "zz9"}, "error"
    ),
    Scenario("B.query.threads", "ppsspp_query", {"action": "threads"}),
    Scenario("B.query.modules", "ppsspp_query", {"action": "modules"}),
    Scenario("B.query.funcs", "ppsspp_query", {"action": "funcs", "top_n": 5}),
    Scenario("B.query.func_scan", "ppsspp_query", {"action": "func_scan", "address": "__PC__"}),
    Scenario("B.query.unknown_action", "ppsspp_query", {"action": "teleport"}, "error"),
    Scenario("B.get_pc", "ppsspp_get_pc", {}, note="capture pc (running)"),
    Scenario("B.step.pause", "ppsspp_step", {"action": "pause"}),
    Scenario("B.disassemble.at_pc", "ppsspp_disassemble", {"address": "__PC__", "count": 8}),
    Scenario(
        "B.disassemble.zero_count",
        "ppsspp_disassemble",
        {"address": "__PC__", "count": 0},
        "ok",
        note="count=0 accepted or error — record semantics",
    ),
    Scenario(
        "B.disassemble.bad_addr",
        "ppsspp_disassemble",
        {"address": "not_an_address", "count": 4},
        "error",
    ),
    Scenario(
        "B.step.into",
        "ppsspp_step",
        {"action": "into"},
        expect="either",  # noqa: E501
        note="F-4: PPSSPP stepping no-op at idle loop — fast decisive "
        "failure is the ratified behavior",
    ),
    Scenario(
        "B.step.pc_after_into",
        "ppsspp_get_pc",
        {},
        expect="either",  # noqa: E501
        note="pc advance not guaranteed (F-4 stepping no-op at idle loop)",
    ),
    Scenario("B.evaluate.pc", "ppsspp_evaluate", {"expression": "pc"}),
    Scenario("B.evaluate.bad_expr", "ppsspp_evaluate", {"expression": "1 +"}, "error"),
    Scenario(
        "B.reg.a1_orig",
        "ppsspp_query",
        {"action": "register", "name": "a1"},
        note="capture a1 original value for restore",
    ),
    Scenario("B.reg.a1_write", "ppsspp_write_register", {"name": "a1", "value": "0x1234"}),
    Scenario(
        "B.reg.a1_readback",
        "ppsspp_query",
        {"action": "register", "name": "a1"},
        note="value may be rewritten by the game between the two "
        "pause windows (tool calls are independent pause/resume cycles)",
    ),
    Scenario(
        "B.reg.a1_restore",
        "ppsspp_write_register",
        {"name": "a1", "value": "__R3_ORIG__"},
        "ok",
        note="dependent on a1_orig capture (blocked by F-2 enum bug)",
    ),
    Scenario(
        "B.reg.write_bad_name", "ppsspp_write_register", {"name": "zz9", "value": "0x0"}, "error"
    ),
    Scenario(
        "B.reg.write_numeric_name",
        "ppsspp_write_register",
        {"name": "r3", "value": "0x0"},
        note="F-5: numeric name r3 is translated to ABI name v1",
    ),
    Scenario(
        "B.search_disasm.jr_ra",
        "ppsspp_search_disasm",
        {"address": "0x08804000", "match": "jr ra", "max_results": 5},
    ),
    Scenario(
        "B.search_disasm.nomatch",
        "ppsspp_search_disasm",
        {"address": "0x08804000", "match": "zzzzz_no_such_insn"},
    ),
    Scenario("B.assemble.nop", "ppsspp_assemble", {"address": SCRATCH, "code": "nop"}),
    Scenario(
        "B.assemble.readback",
        "ppsspp_read_memory",
        {"action": "read_bytes", "address": SCRATCH, "size": 4},
    ),
    Scenario(
        "B.assemble.bad_code",
        "ppsspp_assemble",
        {"address": SCRATCH, "code": "frobnicate r9"},
        "error",
    ),
    Scenario("B.bp.cpu_set", "ppsspp_breakpoint", {"action": "set", "address": SCRATCH}),
    Scenario("B.bp.cpu_list", "ppsspp_breakpoint", {"action": "list"}, validator=_v_bp_in_list),
    Scenario(
        "B.bp.cpu_update",
        "ppsspp_breakpoint",
        {"action": "update", "address": SCRATCH, "enabled": False},
    ),
    Scenario("B.bp.cpu_remove", "ppsspp_breakpoint", {"action": "remove", "address": SCRATCH}),
    Scenario(
        "B.bp.cpu_remove_again",
        "ppsspp_breakpoint",
        {"action": "remove", "address": SCRATCH},
        "error",
        note="R12 定版: 同一断点的第二次 remove 被 F-9 确定性拒绝"
        "（no CPU breakpoint — 不再视为幂等）",
    ),
    Scenario(
        "B.bp.mem_set",
        "ppsspp_breakpoint",
        {"action": "mem_set", "address": SCRATCH, "size": 4, "read": True, "write": True},
    ),
    Scenario("B.bp.mem_list", "ppsspp_breakpoint", {"action": "mem_list"}, validator=_v_bp_in_list),
    Scenario("B.bp.mem_remove", "ppsspp_breakpoint", {"action": "mem_remove", "address": SCRATCH}),
    # ── F-01 fix contracts on the real wire (2026-09-08) ──
    Scenario(
        "B.bp.mem_set_zero_size",
        "ppsspp_breakpoint",
        {"action": "mem_set", "address": SCRATCH, "size": 0},
        "error",
        validator=_v_bp_size_guard,
        note="F-01 fixed: zero-width memcheck rejected BEFORE PPSSPP",
    ),
    Scenario(
        "B.bp.mem_set_size_64",
        "ppsspp_breakpoint",
        {"action": "mem_set", "address": SCRATCH, "size": 64},
        "ok",
        validator=_v_has_keys("action", "breakpoints"),
        note="positive sizes beyond {1,2,4} stay legal (range watch)",
    ),
    Scenario(
        "B.bp.mem_list_64",
        "ppsspp_breakpoint",
        {"action": "mem_list"},
        "ok",
        validator=_v_mem_list_size(64),
        note="the size=64 watch is listed with its real size",
    ),
    Scenario(
        "B.bp.mem_remove_size_64",
        "ppsspp_breakpoint",
        {"action": "mem_remove", "address": SCRATCH, "size": 64},
        "ok",
        validator=_v_has_keys("action", "breakpoints"),
        note="removal by the exact address+size pair",
    ),
    Scenario("B.bp.unknown_action", "ppsspp_breakpoint", {"action": "explode"}, "error"),
    Scenario("B.step.resume", "ppsspp_step", {"action": "resume"}),
    Scenario(
        "B.step.pause_twice",
        "ppsspp_step",
        {"action": "pause"},
        "ok",
        note="R12 定版: 二次 pause 幂等（stepping=True 再确认）",
    ),
    Scenario(
        "B.step.resume_twice",
        "ppsspp_step",
        {"action": "resume"},
        "ok",
        note="R12 定版: 运行态 resume 立即确认 stepping=False",
    ),
    Scenario("B.step.bad_action", "ppsspp_step", {"action": "warp"}, "error"),
    Scenario(
        "B.input.press_cross",
        "ppsspp_press_button",
        {"button": "cross", "duration": 2},
        frame_budget=True,
        note="R19: ticket echoes after duration GAME frames",
    ),
    Scenario(
        "B.input.press_bad_button", "ppsspp_press_button", {"button": "x", "duration": 1}, "error"
    ),
    Scenario("B.input.hold", "ppsspp_hold_buttons", {"buttons": "cross"}),
    Scenario(
        "B.input.hold_release_all",
        "ppsspp_hold_buttons",
        {"buttons": ""},
        note="empty combo = release-all contract",
    ),
    Scenario(
        "B.input.hold_bad_token", "ppsspp_hold_buttons", {"buttons": "cross|joystick"}, "error"
    ),
    Scenario("B.input.analog_center_ok", "ppsspp_send_analog", {"x": 200, "y": 100}),
    Scenario("B.input.analog_out_of_range", "ppsspp_send_analog", {"x": 256, "y": 0}, "error"),
    Scenario("B.input.analog_negative", "ppsspp_send_analog", {"x": -1, "y": 0}, "error"),
    Scenario("B.wait_frames.5", "ppsspp_wait_frames", {"frames": 5}, frame_budget=True),
    Scenario(
        "B.wait_frames.zero",
        "ppsspp_wait_frames",
        {"frames": 0},
        "ok",
        note="R12 定版: frames=0 合法（W3 校验 0..cap）",
    ),
    Scenario("B.wait_frames.negative", "ppsspp_wait_frames", {"frames": -3}, "error"),
    Scenario(
        "B.batch_step.mixed",
        "ppsspp_batch_step",
        {
            "steps": [
                {"type": "press", "button": "cross", "duration": 1},
                {"type": "wait", "frames": 2},
                {"type": "state_probe", "name": "game_mode"},
            ],
            "on_failure": "abort",
        },
        frame_budget=True,
        note="R19: press ticket + wait advance with GAME frames",
    ),
    Scenario("B.batch_step.empty", "ppsspp_batch_step", {"steps": []}, "error"),
    Scenario(
        "B.batch_step.bad_type", "ppsspp_batch_step", {"steps": [{"type": "teleport"}]}, "error"
    ),
    Scenario("B.observer.list", "ppsspp_state_observer", {"action": "list"}),
    Scenario(
        "B.observer.observe_game_mode",
        "ppsspp_state_observer",
        {"action": "observe", "name": "game_mode", "samples": 2},
    ),
    Scenario(
        "B.observer.register_scratch",
        "ppsspp_state_observer",
        {
            "action": "register",
            "name": "harness_scratch",
            "address": SCRATCH,
            "size": 4,
            "description": "harness probe",
        },
    ),
    Scenario(
        "B.observer.observe_scratch",
        "ppsspp_state_observer",
        {"action": "observe", "name": "harness_scratch", "samples": 1},
    ),
    # ── R-A: frame_snapshot contracts (v4 report G-1). Placed here so
    # B.fs.probes_known can reuse the registered harness_scratch probe.
    Scenario(
        "B.fs.snapshot",
        "ppsspp_frame_snapshot",
        {},
        "ok",
        validator=_v_fs_running,
        note="running CPU: pause→capture→resume (resumed=true)",
    ),
    Scenario(
        "B.fs.stays_paused.pause_for", "ppsspp_step", {"action": "pause"}, "ok", note="R-A setup"
    ),
    Scenario(
        "B.fs.stays_paused",
        "ppsspp_frame_snapshot",
        {},
        "ok",
        validator=_v_fs_stays_paused,
        note="already-paused CPU must STAY paused (resumed=false)",
    ),
    Scenario(
        "B.fs.stays_paused.resume_after",
        "ppsspp_step",
        {"action": "resume"},
        "ok",
        note="R-A teardown",
    ),
    Scenario(
        "B.fs.no_regs",
        "ppsspp_frame_snapshot",
        {"want_registers": False},
        "ok",
        validator=_v_fs_no_regs,
        note="F-08 contract: registers key carries null when opted out",
    ),
    Scenario(
        "B.fs.probes_known",
        "ppsspp_frame_snapshot",
        {"probes": "harness_scratch"},
        "ok",
        validator=_v_fs_probes,
        note="observer-registered probe captured alongside CPU state",
    ),
    Scenario(
        "B.observer.clear_scratch",
        "ppsspp_state_observer",
        {"action": "clear", "name": "harness_scratch"},
    ),
    Scenario(
        "B.observer.observe_unknown",
        "ppsspp_state_observer",
        {"action": "observe", "name": "nope"},
        "error",
    ),
    Scenario("B.image.screenshot", "ppsspp_screenshot", {}, validator=_v_image_present),
    Scenario(
        "B.image.dump_texture",
        "ppsspp_dump_texture",
        {},
        "either",
        note="R12 保留 either: GPU 纹理绑定状态环境相关——运行画面有"
        "绑定则 ok，无绑定则 F-10 CAPTURE_EMPTY（两条路径均已批准）",
    ),
    Scenario(
        "B.smoke_test.full",
        "ppsspp_smoke_test",
        {},
        note="full battery: ISO loaded / CPU running / WS healthy",
    ),
    Scenario(
        "B.run_script.hello",
        "ppsspp_run_script",
        {"name": "hello_diagnostic"},
        note="manifest-exposed offline script happy path",
    ),
    # ── R-F: dynamic ppsspp_script_* tools on the real wire (G-3) ──
    Scenario(
        "B.script.tool.check_cpu_state",
        "ppsspp_script_check_cpu_state",
        {},
        "ok",
        validator=_v_script_output,
        note="R-F: dynamic exposed tool (requires_ppsspp) — session_id auto-injected",
    ),
    Scenario(
        "B.script.tool.find_0e_source",
        "ppsspp_script_find_0e_source",
        {},
        "either",
        validator=_v_script_output,
        note="R-F + F-11: dynamic exposed tool; its default data-file "
        "path is workspace-dependent (extract dir absent here → "
        "[FILE_NOT_FOUND]) — the registration/error channel is "
        "what this scenario locks",
    ),
    Scenario(
        "B.image.dump_clut",
        "ppsspp_dump_clut",
        {},
        "either",
        note="R12 保留 either: GPU CLUT 绑定状态环境相关（同 "
        "dump_texture），ok 与 F-10 CAPTURE_EMPTY 两路径均批准",
    ),
    Scenario("B.gpu_stats", "ppsspp_gpu_stats", {}),
    Scenario("B.gpu_record", "ppsspp_gpu_record", {}, note="ticket async — one frame GE dump"),
    Scenario("B.mem_info_search.Game", "ppsspp_memory_info_search", {"match": "Game"}),
    Scenario(
        "B.mem_info_search.empty",
        "ppsspp_memory_info_search",
        {"match": ""},
        "error",
        note="O2 定版: F-11b 空/空白 match 确定性抛 ToolError（空串会"
        "匹配一切）——不再记录语义，直接断言拒绝",
    ),
    Scenario("B.replay.status", "ppsspp_replay", {"action": "status"}),
    Scenario(
        "B.replay.time_get",
        "ppsspp_replay",
        {"action": "time_get"},
        "ok",
        note="R12 定版: 游戏运行中 PSP 已初始化",
    ),
    Scenario(
        "B.replay.begin_abort",
        "ppsspp_replay",
        {"action": "begin"},
        "ok",
        note="R12 定版: 运行中 begin 必成功（随后 abort 清理）",
    ),
    Scenario(
        "B.replay.abort", "ppsspp_replay", {"action": "abort"}, "ok", note="R12 定版: abort 幂等"
    ),
    Scenario("B.replay.execute_no_data", "ppsspp_replay", {"action": "execute"}, "error"),
    # ── F-02 fix contracts on the real wire (2026-09-08) ──
    Scenario(
        "B.replay.begin_for_save",
        "ppsspp_replay",
        {"action": "begin"},
        "ok",
        validator=_v_has_keys("action"),
        note="F-02 wire: fresh recorder (no frames elapsed yet)",
    ),
    Scenario(
        "B.replay.save_empty_rejected",
        "ppsspp_replay",
        {"action": "save", "file_path": "harness_empty_capture.ppr"},
        "error",
        validator=_v_replay_empty_rejected,
        note="F-02 fixed: empty capture → [REPLAY_EMPTY], no .ppr "
        "written (begin→save back-to-back = 0 frames)",
    ),
    Scenario(
        "B.replay.abort_after_save",
        "ppsspp_replay",
        {"action": "abort"},
        "ok",
        validator=_v_has_keys("action"),
    ),
    Scenario(
        "B.analyze_log.real",
        "ppsspp_analyze_log",
        {"log_path": str(ANALYZE_FIXTURE_LOG), "filter": "ERROR"},
    ),
    Scenario("B.convert.auto", "ppsspp_convert_address", {"address": "0x08A0D000", "mode": "auto"}),
    Scenario(
        "B.read.string_code_unsafe",
        "ppsspp_read_memory",
        {"action": "read_string", "address": "0x08804000"},
        "ok",
        note="O1 定版: F-3 后 read_string 走有界读+本地 NUL 截断，"
        "永不调用 strnlen 事件——代码段读取确定性成功",
    ),
    Scenario("B.session.get", "ppsspp_session", {"action": "get", "session_id": "__SESSION_ID__"}),
    Scenario("B.session_list.one", "ppsspp_session_list", {}),
    # ── V2 additions (post-review-fix contracts, 2026-09-06 round 2) ──
    Scenario(
        "B.v2.write_long_string",
        "ppsspp_write_memory",
        {"address": SCRATCH, "format": "bytes", "data": _LONG_STR_HEX},
        note="seed 8000 'A' + NUL at scratch — proves >4096 strings",
    ),
    Scenario(
        "B.v2.read_string_64k",
        "ppsspp_read_memory",
        {"action": "read_string", "address": SCRATCH, "max_len": 65536},
        note="W1: max_len=65536 must NOT be re-clamped to 4096",
    ),
    Scenario(
        "B.v2.write_protected_boundary",
        "ppsspp_write_memory",
        {"address": "0x08803FFD", "data": "0x11223344", "format": "u32"},
        "error",
        note="W2: u32 crossing into 0x08804000 must hit the guard",
    ),
    Scenario(
        "B.v2.write_empty_bytes",
        "ppsspp_write_memory",
        {"address": SCRATCH, "data": "", "format": "bytes"},
        "error",
        note="建议6: zero-byte write must be rejected",
    ),
    Scenario(
        "B.v2.wait_frames_zero_interval",
        "ppsspp_wait_frames",
        {"frames": 60, "interval": 0},
        "error",
        note="W3: interval<=0 must be rejected (hot-loop guard)",
    ),
    Scenario(
        "B.v2.wait_frames_over_cap",
        "ppsspp_wait_frames",
        {"frames": 1000000000},
        "error",
        note="W3: frames beyond the cap must be rejected",
    ),
    Scenario(
        "B.v2.scan_big_chunk",
        "ppsspp_read_memory",
        {
            "action": "scan",
            "pattern": "DEADBEEF",
            "start_addr": "0x08800000",
            "end_addr": "0x08900000",
            "chunk_size": 536870912,
        },
        note="W4: 512MB chunk_size is clamped to 64KiB, scan succeeds",
    ),
    Scenario(
        "B.v2.scan_range_over_cap",
        "ppsspp_read_memory",
        {
            "action": "scan",
            "pattern": "DEADBEEF",
            "start_addr": "0x08800000",
            "end_addr": "0x88800000",
        },
        "error",
        note="W4: 2GiB scan range rejected (256MiB cap)",
    ),
    Scenario(
        "B.v2.replay_save_traversal",
        "ppsspp_replay",
        {"action": "save", "file_path": "../evil.ppr"},
        "error",
        note="S2: path traversal must be rejected before session I/O",
    ),
    Scenario(
        "B.v2.analyze_outside_whitelist",
        "ppsspp_analyze_log",
        {"log_path": "C:/Windows/win.ini", "filter": "ERROR"},
        "error",
        note="S3: arbitrary file read must be refused",
    ),
    Scenario(
        "B.v2.write_reg_dollar",
        "ppsspp_write_register",
        {"name": "$a0", "value": "0x2A"},
        note="W11: '$a0' must be normalized, not rejected by PPSSPP",
    ),
    Scenario(
        "B.v2.query_reg_dollar",
        "ppsspp_query",
        {"action": "register", "name": "$a0"},
        note="W11: read-back of the normalized '$a0' write",
    ),
    Scenario(
        "B.v2.convert_oversized",
        "ppsspp_convert_address",
        {"address": "0x123456789"},
        "error",
        note="W13: >32-bit address rejected",
    ),
    Scenario(
        "B.v2.convert_negative",
        "ppsspp_convert_address",
        {"address": "-5"},
        "error",
        note="W13: negative address rejected",
    ),
    # ── V3 additions (review-r2 behavioral contracts, round 3) ──
    Scenario(
        "B.v3.scan_pattern_over_cap",
        "ppsspp_read_memory",
        {
            "action": "scan",
            "pattern": "41" * 4098,  # 4098 bytes > 4096 cap
            "start_addr": "0x08800000",
            "end_addr": "0x08810000",
        },
        "error",
        note="S1: oversized pattern fails fast naming the scan cap, not a silent empty scan",
    ),
    Scenario(
        "B.v3.mem_remove_explicit_size_missing",
        "ppsspp_breakpoint",
        {"action": "mem_remove", "address": "0x09FE0040", "size": 16},
        "error",
        note="S3: explicit non-default size + no memcheck = loud "
        "error (the old silent-remove carve-out is gone)",
    ),
    Scenario(
        "B.v3.analyze_default_mirror",
        "ppsspp_analyze_log",
        {"filter": "ERROR"},
        note="W4: log_path omitted reads the mirrored ppsspp.log "
        "(pre-seeded ERROR line must be found)",
    ),
    Scenario(
        "B.v3.screenshot_empty_flag",
        "ppsspp_screenshot",
        {},
        note="S2: metadata JSON carries the explicit 'empty' flag",
    ),
    Scenario(
        "B.v3.batch_step_failure_summary",
        "ppsspp_batch_step",
        {"steps": [{"type": "state_probe", "names": "no_such_probe"}], "on_failure": "continue"},
        "error",
        note="S6: BATCH_STEP_FAILED message carries a compact "
        "step[0] summary, not the old details= JSON envelope",
    ),
    Scenario(
        "B.v3.session_busy_race",
        "ppsspp_wait_frames",
        {
            "__race__": {
                "holder": {
                    "tool": "ppsspp_batch_step",
                    "args": {
                        "session_id": "__SESSION_ID__",
                        "steps": [{"type": "wait", "frames": 480}],
                    },
                },  # 8s lock hold
                "victim": {
                    "tool": "ppsspp_read_memory",
                    "args": {
                        "session_id": "__SESSION_ID__",
                        "action": "read_u32",
                        "address": "0x08804000",
                    },
                },
                "delay_s": 0.5,
            }
        },
        "ok",
        note="W1: concurrent quick read behind an 8s holder gets "
        "SESSION_BUSY after the 5s timeout; holder still ok",
    ),
    # ── R-A/R-C: hygiene gate before teardown (probe P1.hygiene.final) ──
    Scenario(
        "B.hygiene.mem_bp_empty",
        "ppsspp_breakpoint",
        {"action": "mem_list"},
        "ok",
        validator=_v_bp_mem_list_empty,
        note="no memory breakpoint may survive full phase B",
    ),
    Scenario(
        "B.hygiene.observer_clean",
        "ppsspp_state_observer",
        {"action": "list"},
        "ok",
        validator=_v_observer_no_scratch,
        note="harness-registered probes must be cleared",
    ),
    Scenario(
        "B.hygiene.smoke_liveness",
        "ppsspp_smoke_test",
        {},
        "ok",
        validator=_v_smoke_liveness,
        note="R-C three-check liveness (iso/cpu/ws) — game_mode_valid "
        "excluded as game-phase dependent (F-07)",
    ),
    Scenario(
        "B.session.stop", "ppsspp_session", {"action": "stop", "session_id": "__SESSION_ID__"}
    ),
    Scenario(
        "B.post_stop.read",
        "ppsspp_read_memory",
        {"action": "read_bytes", "address": "0x08804000", "size": 4},
        "error",
        note="guard after stop",
    ),
    Scenario("B.post_stop.session_list", "ppsspp_session_list", {}),
]

# Wire the VALIDATORS registry onto scenario objects (id-keyed).
for _sc in (*PHASE_A, *PHASE_B):
    if _sc.validator is None and _sc.id in VALIDATORS:
        _sc.validator = VALIDATORS[_sc.id]


# ── R-B batch 1 (2026-09-08): anti hollow-ok shape validators ──────────────
# v4 report G-2: 43% of expect=ok scenarios asserted nothing about the
# payload. This id-keyed map adds server-constructed SHAPE assertions
# (keys / hex-shape / non-null payload) — never game-semantic values,
# so game state drift cannot false-positive (R-B risk note). Counted
# by scripts/audit_test_modules.py (target: ok-without-validator ≤ 20%).
_RB_VALIDATORS: dict[str, Callable] = {
    # phase A surface
    "A.health.ok": _v_has_keys("status", "tool_count", "session_count"),
    "A.session_list.empty": _v_has_keys("sessions"),
    "A.convert.ida_to_ppsspp.zero": _v_has_keys("original", "converted", "mode"),
    "A.convert.ppsspp_to_ida.base": _v_has_keys("original", "converted", "mode"),
    "A.list_addresses.default": _v_has_keys("sections", "count"),
    "A.list_scripts": _v_exposed_scripts,
    "A.reload_scripts": _v_has_keys("reloaded_count", "exposed_count"),
    "A.guard.ppsspp_state_observer": _v_has_keys("action", "probes"),
    # step family (StepResponse)
    "B.wb.pause_for": _v_has_keys("action"),
    "B.wb.resume_after": _v_has_keys("action"),
    "B.trace.pause_for": _v_has_keys("action"),
    "B.trace.resume_after": _v_has_keys("action"),
    "B.fs.stays_paused.pause_for": _v_has_keys("action"),
    "B.fs.stays_paused.resume_after": _v_has_keys("action"),
    "B.step.pause": _v_has_keys("action"),
    "B.step.resume": _v_has_keys("action"),
    "B.step.pause_twice": _v_has_keys("action"),
    "B.step.resume_twice": _v_has_keys("action"),
    # memory reads (MemoryReadResponse)
    "B.read.bytes.code": _v_has_keys("action", "address"),
    "B.read.scan_prologue": _v_has_keys("action", "address"),
    "B.read.string_code_unsafe": _v_has_keys("action", "address"),
    "B.v2.read_string_64k": _v_has_keys("action", "address"),
    "B.v2.scan_big_chunk": _v_has_keys("action", "address"),
    # memory writes (MemoryWriteResponse: address/format/bytes_written)
    "B.write.string_seed": _v_has_keys("address", "bytes_written"),
    "B.write.u8": _v_has_keys("address", "bytes_written"),
    "B.write.u16": _v_has_keys("address", "bytes_written"),
    "B.write.u32": _v_has_keys("address", "bytes_written"),
    "B.write.bytes": _v_has_keys("address", "bytes_written"),
    "B.v2.write_long_string": _v_has_keys("address", "bytes_written"),
    # query (QueryResponse: action/data)
    "B.query.game_state": _v_data_present,
    "B.query.registers": _v_data_present,
    "B.query.threads": _v_data_present,
    "B.query.modules": _v_data_present,
    "B.query.funcs": _v_data_present,
    "B.query.func_scan": _v_data_present,
    "B.query.register.pc": _v_reg_query,
    "B.query.register.a0": _v_reg_query,
    "B.reg.a1_readback": _v_reg_query,
    "B.v2.query_reg_dollar": _v_reg_query,
    # disasm / evaluate / registers / search / assemble
    "B.disassemble.at_pc": _v_has_keys("address", "count", "instructions"),
    "B.disassemble.zero_count": _v_has_keys("address", "count", "instructions"),
    "B.evaluate.pc": _v_value_hex,
    "B.reg.a1_write": _v_has_keys("name", "value"),
    "B.reg.a1_restore": _v_has_keys("name", "value"),
    "B.reg.write_numeric_name": _v_has_keys("name", "value"),
    "B.v2.write_reg_dollar": _v_has_keys("name", "value"),
    "B.search_disasm.jr_ra": _v_has_keys("address", "results"),
    "B.search_disasm.nomatch": _v_has_keys("address", "results"),
    "B.assemble.nop": _v_has_keys("address", "bytes_written"),
    # breakpoints (BreakpointResponse: action/breakpoints)
    "B.bp.cpu_set": _v_has_keys("action", "breakpoints"),
    "B.bp.cpu_update": _v_has_keys("action", "breakpoints"),
    "B.bp.cpu_remove": _v_has_keys("action", "breakpoints"),
    "B.bp.mem_set": _v_has_keys("action", "breakpoints"),
    "B.bp.mem_remove": _v_has_keys("action", "breakpoints"),
    # input
    "B.input.press_cross": _v_has_keys("button", "duration"),
    "B.input.hold": _v_has_keys("buttons"),
    "B.input.hold_release_all": _v_has_keys("buttons"),
    "B.input.analog_center_ok": _v_has_keys("x", "y"),
    # waits (WaitFramesResponse)
    "B.wait_frames.5": _v_has_keys("frames", "elapsed_s"),
    "B.wait_frames.zero": _v_has_keys("frames", "elapsed_s"),
    # observer (StateObserverResponse)
    "B.observer.list": _v_has_keys("action", "probes"),
    "B.observer.observe_game_mode": _v_has_keys("action"),
    "B.observer.register_scratch": _v_has_keys("action"),
    "B.observer.observe_scratch": _v_has_keys("action"),
    "B.observer.clear_scratch": _v_has_keys("action"),
    # capture / gpu / misc
    "B.smoke_test.full": _v_smoke_liveness,
    "B.run_script.hello": _v_script_output,
    "B.gpu_stats": _v_has_keys("fps"),
    "B.gpu_record": _v_has_keys("size_bytes"),
    "B.mem_info_search.Game": _v_has_keys("regions"),
    # replay (ReplayResponse)
    "B.replay.status": _v_has_keys("action"),
    "B.replay.time_get": _v_has_keys("action"),
    "B.replay.begin_abort": _v_has_keys("action"),
    "B.replay.abort": _v_has_keys("action"),
    "B.analyze_log.real": _v_has_keys("log_path", "count"),
    "B.convert.auto": _v_has_keys("original", "converted", "mode"),
    "B.session.get": _v_has_keys("session_id"),
    "B.session.stop": _v_has_keys("session_id"),
    "B.session_list.one": _v_has_keys("sessions"),
    "B.post_stop.session_list": _v_has_keys("sessions"),
    "B.v3.session_busy_race": _v_race_contract,
}
for _sc in (*PHASE_A, *PHASE_B):
    if _sc.validator is None and _sc.id in _RB_VALIDATORS:
        _sc.validator = _RB_VALIDATORS[_sc.id]


# ── Runner ──────────────────────────────────────────────────────────────────


async def _call(session: ClientSession, sc: Scenario, phase: str) -> Record:
    rec = Record(id=sc.id, tool=sc.tool, phase=phase, expect=sc.expect, note=sc.note)
    args = subst(sc.args)

    # Auto-inject session_id: the client must pass it per call. Live
    # session id when known; a sentinel during the no-session phase so
    # guards exercise the business SessionNotFound path.
    if sc.tool in SESSION_ID_TOOLS and sc.tool != "ppsspp_session" and "session_id" not in args:
        args["session_id"] = STATE.get("SESSION_ID") or "sess_unknown_guard"

    # Special scenario kinds.
    t0 = time.perf_counter()
    # R-A: MCP surface beyond call_tool. Prompts/resources are
    # session-independent; errors arrive as protocol rejections
    # (classified rpc_error via _is_mcp_error).
    if "__prompts_list__" in args:
        args.pop("__prompts_list__")
        try:
            res = await asyncio.wait_for(session.list_prompts(), timeout=30.0)
            rec.latency_ms = (time.perf_counter() - t0) * 1000
            rec.status = "ok"
            rec.structured = {"prompts": sorted(p.name for p in res.prompts)}
        except Exception as e:  # noqa: BLE001
            rec.latency_ms = (time.perf_counter() - t0) * 1000
            rec.status = "rpc_error" if _is_mcp_error(e) else "exception"
            rec.error = f"{type(e).__name__}: {e}"[:400]
        if sc.validator:
            rec.problems = sc.validator(rec, STATE)
        return rec
    if "__prompt_get__" in args:
        name = args.pop("__prompt_get__")
        pargs = args.pop("__prompt_args__", None)
        try:
            res = await asyncio.wait_for(session.get_prompt(name, pargs), timeout=30.0)
            rec.latency_ms = (time.perf_counter() - t0) * 1000
            texts = [str(m.content) for m in res.messages]
            rec.text_head = "\n".join(texts)[:1200]
            rec.status = "ok" if res.messages else "tool_error"
            rec.structured = {"n_messages": len(res.messages)}
            if rec.status == "tool_error":
                rec.error = "prompt rendered no messages"
        except Exception as e:  # noqa: BLE001
            rec.latency_ms = (time.perf_counter() - t0) * 1000
            rec.status = "rpc_error" if _is_mcp_error(e) else "exception"
            rec.error = f"{type(e).__name__}: {e}"[:400]
        if sc.validator and (
            rec.status == "ok" or sc.expect == "error" and rec.status == "rpc_error"
        ):
            rec.problems = sc.validator(rec, STATE)
        return rec
    if "__resources_list__" in args:
        args.pop("__resources_list__")
        try:
            res = await asyncio.wait_for(session.list_resources(), timeout=30.0)
            rec.latency_ms = (time.perf_counter() - t0) * 1000
            rec.status = "ok"
            rec.structured = {"resources": sorted(str(r.uri) for r in res.resources)}
        except Exception as e:  # noqa: BLE001
            rec.latency_ms = (time.perf_counter() - t0) * 1000
            rec.status = "rpc_error" if _is_mcp_error(e) else "exception"
            rec.error = f"{type(e).__name__}: {e}"[:400]
        if sc.validator:
            rec.problems = sc.validator(rec, STATE)
        return rec
    if "__resource_read__" in args:
        uri = args.pop("__resource_read__")
        try:
            res = await asyncio.wait_for(session.read_resource(uri), timeout=30.0)
            rec.latency_ms = (time.perf_counter() - t0) * 1000
            rec.status = "ok"
            rec.structured = {"n_contents": len(res.contents)}
        except Exception as e:  # noqa: BLE001
            rec.latency_ms = (time.perf_counter() - t0) * 1000
            rec.status = "rpc_error" if _is_mcp_error(e) else "exception"
            rec.error = f"{type(e).__name__}: {e}"[:400]
        if sc.validator and (
            rec.status == "ok" or sc.expect == "error" and rec.status == "rpc_error"
        ):
            rec.problems = sc.validator(rec, STATE)
        return rec

    if "__burst__" in args:
        n = args.pop("__burst__")
        ok = limited = 0
        t0 = time.perf_counter()
        for _ in range(n):
            try:
                r = await asyncio.wait_for(
                    session.call_tool(sc.tool, {"section": None}), timeout=30.0
                )
                if getattr(r, "is_error", False) or "RATE_LIMIT" in str(getattr(r, "content", "")):
                    limited += 1
                else:
                    ok += 1
            except Exception:
                limited += 1
        rec.latency_ms = (time.perf_counter() - t0) * 1000
        rec.status = "ok"
        rec.structured = {"burst": n, "ok": ok, "rate_limited": limited}
        if sc.validator:
            rec.problems = sc.validator(rec, STATE)
        return rec

    if "__race__" in args:
        # W1 v3 scenario: two CONCURRENT tool calls against the same
        # session. The holder (long wait_frames) owns the per-session
        # lock for ~8s; the victim (quick read) must fail with
        # SESSION_BUSY after the 5s busy timeout instead of silently
        # queueing — and the holder must still complete successfully.
        race = args.pop("__race__")
        holder_task = asyncio.create_task(
            session.call_tool(race["holder"]["tool"], race["holder"]["args"])
        )
        await asyncio.sleep(race.get("delay_s", 0.5))
        t0 = time.perf_counter()
        try:
            vres = await asyncio.wait_for(
                session.call_tool(race["victim"]["tool"], race["victim"]["args"]), timeout=30.0
            )
            victim_err = getattr(vres, "is_error", False)
            vtext = str(getattr(vres, "content", ""))[:300]
            rec.latency_ms = (time.perf_counter() - t0) * 1000
            victim_status = "tool_error" if victim_err else "ok"
        except Exception as e:
            rec.latency_ms = (time.perf_counter() - t0) * 1000
            victim_status = "exception"
            vtext = f"{type(e).__name__}: {e}"
        hres = await holder_task
        holder_err = getattr(hres, "is_error", False)
        # H1 (2026-09-07): a race may EXPECT the victim to succeed (e.g.
        # read during a lock-free breakpoint wait) instead of getting
        # SESSION_BUSY behind a lock holder.
        victim_expect_error = bool(race.get("victim_expect_error", True))
        victim_wanted = "tool_error" if victim_expect_error else "ok"
        rec.status = "ok" if (victim_status == victim_wanted and not holder_err) else "tool_error"
        rec.structured = {
            "victim_status": victim_status,
            "victim_latency_ms": round(rec.latency_ms, 1),
            "victim_text": vtext[:200],
            "holder_ok": not holder_err,
        }
        rec.note = sc.note
        if sc.validator:
            rec.problems = sc.validator(rec, STATE)
        return rec

    t0 = time.perf_counter()
    try:
        result = await asyncio.wait_for(session.call_tool(sc.tool, args), timeout=CALL_TIMEOUT_S)
        rec.latency_ms = (time.perf_counter() - t0) * 1000
        summarize_content(result, rec)
        err = getattr(result, "is_error", None)
        if err is None:
            err = getattr(result, "isError", False)
        rec.status = "tool_error" if err else "ok"
        if rec.status == "tool_error":
            rec.error = rec.text_head
    except TimeoutError:
        rec.latency_ms = (time.perf_counter() - t0) * 1000
        rec.status = "timeout"
        rec.error = f"call exceeded {CALL_TIMEOUT_S}s"
    except Exception as e:  # McpError and transport-level failures
        rec.latency_ms = (time.perf_counter() - t0) * 1000
        rec.status = "exception" if not _is_mcp_error(e) else "rpc_error"
        rec.error = f"{type(e).__name__}: {e}"[:500]

    _validate_rejection = rec.status == "tool_error" and sc.expect == "error"
    if sc.validator and (rec.status == "ok" or _validate_rejection):
        # v3: rejection records ALSO run validators — several v3
        # contracts assert on the SHAPE of the rejection message
        # (S1/S3/S6/W1), not just that a rejection happened. Scoped to
        # expect=="error" scenarios: an "either" scenario's sanctioned
        # tool_error path (e.g. dump_texture CAPTURE_EMPTY) must not be
        # content-validated (R12 ratified both paths).
        if (
            rec.status == "ok"
            and rec.structured is not None
            and not isinstance(rec.structured, dict)
        ):
            rec.problems = [
                f"validator skipped: structured is {type(rec.structured).__name__}, not dict"
            ]
        else:
            rec.problems = sc.validator(rec, STATE)
    # O3/R11: latency budget gate — a slow-but-successful call is a
    # regression, not a pass. Only phase-B ok calls are gated (phase A
    # guards are dominated by server-side arg validation, no emulator).
    if phase == "B" and rec.status == "ok":
        if sc.frame_budget:
            p50 = _FRAME_P50_BASELINE.get(sc.tool)
            budget = (
                (p50 * 3 + 2000.0)
                if p50 is not None
                else LATENCY_BUDGET_MS.get(sc.tool, LATENCY_BUDGET_DEFAULT_MS)
            )
        else:
            budget = LATENCY_BUDGET_MS.get(sc.tool, LATENCY_BUDGET_DEFAULT_MS)
        if rec.latency_ms > budget:
            kind = "frame-time budget (p50×3+2s)" if sc.frame_budget else "budget"
            rec.problems = list(rec.problems) + [
                f"latency {rec.latency_ms:.0f}ms exceeds {kind} {budget:.0f}ms "
                f"for {sc.tool} (regression gate, report v2 §5)"
            ]
    return rec


async def _boot_wait(session: ClientSession, rec: Record) -> None:
    """H0 (2026-09-07): block on ppsspp_session(action='wait_ready') until
    the emulated CPU is up.

    Replaces the ws_connected poll: PPSSPP answers WebSocket requests
    BEFORE the CPU starts, so ws_connected was a false go-signal and the
    first scenarios could fail with "CPU not started (level=2)".
    wait_ready polls the probe read server-side (100s budget here, within
    the 120s per-call timeout) and fails with [BOOT_TIMEOUT] on wedge
    suspicion — which feeds the caller's boot recovery loop unchanged.
    """
    sid = STATE.get("SESSION_ID")
    if not sid:
        rec.status = "tool_error"
        rec.error = "no session_id captured from start response"
        return
    t0 = time.perf_counter()
    try:
        r = await session.call_tool(
            "ppsspp_session",
            {"action": "wait_ready", "session_id": sid, "timeout_s": 100.0},
        )
    except Exception as e:
        rec.latency_ms = (time.perf_counter() - t0) * 1000
        rec.status = "tool_error"
        rec.error = f"wait_ready raised: {type(e).__name__}: {e}"[:300]
        return
    rec.latency_ms = (time.perf_counter() - t0) * 1000
    err = getattr(r, "is_error", None)
    if err is None:
        err = getattr(r, "isError", False)
    s = getattr(r, "structured_content", None) or {}
    if err:
        rec.status = "tool_error"
        rec.error = (str(getattr(r, "content", "")) or "wait_ready failed")[:300]
        return
    if s.get("ready"):
        rec.status = "ok"
        rec.structured = s
        return
    rec.status = "tool_error"
    rec.error = f"wait_ready returned ready={s.get('ready')!r}"


async def _capture_state(session: ClientSession) -> None:
    """Best-effort capture of pc / r3 originals for later substitution.

    Defensive: a failing capture must not crash the run; affected
    scenarios will fail individually instead.
    """
    try:
        r = await session.call_tool("ppsspp_get_pc", {"session_id": STATE.get("SESSION_ID")})
        s = getattr(r, "structured_content", None) or {}
        STATE.setdefault("PC", _hex_of(s.get("pc", s.get("value", ""))))
    except Exception as e:
        print(f"  (capture pc failed: {type(e).__name__})")


async def _stop_all_sessions(session: ClientSession) -> int:
    """R17: clear stale sessions left by a previous (possibly wedged)
    harness/server run — delegated to the shared runner lib (R-E)."""
    return await _wire_stop_all_sessions(session)


async def run(phase: str) -> dict[str, Any]:
    env_params = build_stdio_params(PYTHON_EXE)
    records: list[Record] = []
    meta: dict[str, Any] = {}
    errlog_path = REPORT_PATH.parent / "server_stderr.log"
    errlog_path.parent.mkdir(parents=True, exist_ok=True)
    errlog_file = errlog_path.open("w", encoding="utf-8", errors="replace")
    async with (
        stdio_client(env_params, errlog=errlog_file) as (read, write),
        ClientSession(read, write) as session,
    ):
        t0 = time.perf_counter()
        init = await asyncio.wait_for(session.initialize(), timeout=60.0)
        meta["handshake_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        meta["protocol_version"] = getattr(init, "protocol_version", None) or getattr(
            init, "protocolVersion", None
        )
        sinfo = getattr(init, "server_info", None) or getattr(init, "serverInfo", None)
        meta["server_info"] = {
            "name": getattr(sinfo, "name", None),
            "version": getattr(sinfo, "version", None),
        }
        t0 = time.perf_counter()
        tools = await asyncio.wait_for(session.list_tools(), timeout=30.0)
        meta["list_tools_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        meta["tool_count"] = len(tools.tools)
        meta["tool_names"] = sorted(t.name for t in tools.tools)
        SESSION_ID_TOOLS.update(
            t.name
            for t in tools.tools
            if "session_id"
            in (
                (getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}).get(
                    "properties", {}
                )
            )
            and t.name != "ppsspp_session"
        )
        meta["python"] = platform.python_version()
        meta["iso"] = ISO_PATH

        # F-06 / R-A: pre-clean stale sessions BEFORE phase A too —
        # a leftover session (or a disk-restored one) makes the
        # no-session guards and the resource single-session guard
        # non-deterministic.
        stale_pre = await _stop_all_sessions(session)
        if stale_pre:
            print(f"  [pre-clean] stopped {stale_pre} stale session(s) before phase A")
            sys.stdout.flush()

        phases = {"a": [PHASE_A], "b": [PHASE_B]}.get(phase, [PHASE_A, PHASE_B])
        _START_SCENARIO = next(s for s in PHASE_B if s.id == "B.session.start")
        recovers_since_start = 0
        for phase_list in phases:
            pname = "A" if phase_list is PHASE_A else "B"
            for sc in phase_list:
                if sc.id == "B.session.start":
                    STATE.clear()
                    stale = await _stop_all_sessions(session)
                    if stale:
                        print(f"  [pre-clean] stopped {stale} stale session(s) from a previous run")
                        sys.stdout.flush()
                rec = await _call(session, sc, pname)
                if sc.id == "B.session.start" and rec.status == "ok":
                    # R17 (2026-09-06): wedge detection + recovery.
                    # PPSSPP intermittently wedges at launch (TCP
                    # listens but the main thread is stuck in GPU
                    # device creation, so ws_connected never comes).
                    # Previously one dead boot poisoned all ~170
                    # subsequent scenarios. Now: on boot_wait
                    # timeout, force-stop the wedged PPSSPP and
                    # re-launch (up to 2 recoveries).
                    recoveries = 0
                    while True:
                        rec2 = Record(
                            id="B.session.boot_wait",
                            tool="(poll)",
                            phase="B",
                            expect="ok",
                            note="ws_connected poll",
                        )
                        await _boot_wait(session, rec2)
                        records.append(rec2)
                        if rec2.status == "ok" or recoveries >= 2:
                            break
                        recoveries += 1
                        print(
                            f"  [boot-recovery {recoveries}] boot_wait "
                            f"timed out — restarting wedged PPSSPP session"
                        )
                        sys.stdout.flush()
                        meta.setdefault("boot_recovery", []).append(
                            {
                                "attempt": recoveries,
                                "boot_wait_error": rec2.error,
                            }
                        )
                        await session.call_tool(
                            "ppsspp_session",
                            {"action": "stop", "session_id": STATE.get("SESSION_ID")},
                        )
                        await asyncio.sleep(3.0)
                        rec = await _call(session, sc, pname)
                        records.append(rec)
                        mark = "PASS" if rec.passed else "FAIL"
                        print(
                            f"[{mark}] {rec.id} (restart x{recoveries}, "
                            f"{rec.status}, {rec.latency_ms:.0f}ms)"
                        )
                        if rec.status != "ok":
                            break
                    meta["boot_recovery_attempts"] = recoveries
                    if rec2.status == "ok":
                        rec3 = Record(
                            id="B.session.boot_settle",
                            tool="(sleep)",
                            phase="B",
                            expect="ok",
                            note="title screen settle",
                        )
                        await asyncio.sleep(12.0)
                        rec3.status = "ok"
                        rec3.latency_ms = 12000.0
                        records.append(rec3)
                        await _capture_state(session)
                records.append(rec)
                mark = "PASS" if rec.passed else "FAIL"
                print(
                    f"[{mark}] {rec.id} ({rec.status}, {rec.latency_ms:.0f}ms)"
                    + (f" — {rec.error[:120]}" if rec.error and not rec.passed else "")
                )
                sys.stdout.flush()
                # R17 supplement: PPSSPP occasionally DIES mid-run
                # (dying-gasps latency spike, then process exit; idle
                # GC then removes the session → SESSION_NOT_FOUND on
                # every later scenario). Without recovery one death
                # fails the remaining ~30 scenarios. The post-stop
                # guards are EXPECTED to see SESSION_NOT_FOUND — skip.
                if (
                    pname == "B"
                    and not rec.passed
                    and "SESSION_NOT_FOUND" in rec.error
                    and not sc.id.startswith("B.post_stop")
                    and sc.id != "B.session.stop"
                    and recovers_since_start < 3
                ):
                    recovers_since_start += 1
                    print(
                        f"  [session-recovery {recovers_since_start}] "
                        f"session lost mid-run — restarting PPSSPP"
                    )
                    sys.stdout.flush()
                    meta.setdefault("session_recovery", []).append(
                        {
                            "after_scenario": sc.id,
                        }
                    )
                    await session.call_tool(
                        "ppsspp_session",
                        {"action": "stop", "session_id": STATE.get("SESSION_ID")},
                    )
                    await asyncio.sleep(3.0)
                    rec_s = await _call(session, _START_SCENARIO, "B")
                    records.append(rec_s)
                    if rec_s.status == "ok":
                        rec2 = Record(
                            id="B.session.boot_wait",
                            tool="(poll)",
                            phase="B",
                            expect="ok",
                            note="ws_connected poll",
                        )
                        await _boot_wait(session, rec2)
                        records.append(rec2)
                        if rec2.status == "ok":
                            await asyncio.sleep(12.0)
                            await _capture_state(session)

    errlog_file.close()

    summary = summarize(records)
    return {"meta": meta, "summary": summary, "records": [vars(r) for r in records]}


def summarize(records: list[Record]) -> dict[str, Any]:
    by_tool: dict[str, list[float]] = {}
    passed = failed = 0
    for r in records:
        by_tool.setdefault(r.tool, []).append(r.latency_ms)
        if r.passed:
            passed += 1
        else:
            failed += 1
    lat = {
        t: {
            "n": len(v),
            "min": round(min(v), 1),
            "avg": round(sum(v) / len(v), 1),
            "max": round(max(v), 1),
        }
        for t, v in sorted(by_tool.items())
    }
    fails = [
        {"id": r.id, "status": r.status, "error": r.error[:200], "problems": r.problems}
        for r in records
        if not r.passed
    ]
    return {"passed": passed, "failed": failed, "latency_by_tool": lat, "failures": fails}


BASELINE_PATH = Path(__file__).resolve().parent / "harness_latency_baseline.json"

# R19: per-tool committed p50 (ms) for frame-time budget computation.
try:
    _FRAME_P50_BASELINE: dict[str, float] = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))[
        "p50_ms"
    ]
except (OSError, json.JSONDecodeError, KeyError):    _FRAME_P50_BASELINE = {}  # gate falls back to the fixed dict budget


def _ok_p50_by_tool(records: list[dict]) -> dict[str, float]:
    """Per-tool p50 latency (ms) over successful phase-B calls (R11)."""
    by_tool: dict[str, list[float]] = {}
    for r in records:
        if r["phase"] == "B" and r["status"] == "ok" and r["latency_ms"] > 0:
            by_tool.setdefault(r["tool"], []).append(r["latency_ms"])
    stats: dict[str, float] = {}
    for tool, lats in by_tool.items():
        lats.sort()
        stats[tool] = round(lats[len(lats) // 2], 1)
    return stats


# R19: tools whose latency is dominated by GAME frame time (press
# tickets echo after N frames; waits sleep N frames). Server overhead is
# a small constant on top, so the committed-baseline threshold for these
# is p50×3 + 2s frame-jitter allowance instead of the tight 3×/+50ms.
_FRAME_TIME_TOOLS: frozenset[str] = frozenset(
    {
        "ppsspp_batch_step",
        "ppsspp_wait_frames",
        "ppsspp_press_button",
    }
)


def _compare_latency_baseline(stats: dict[str, float]) -> list[str]:
    """R11: flag p50 regressions vs the committed baseline (3x baseline or
    +50ms, whichever is larger — jitter-proof). R19: frame-time tools use
    p50×3 + 2s instead (game-frame slowdowns are environment, not code
    regressions — see verification report v3 F-23)."""
    if not BASELINE_PATH.exists():
        return []
    base = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["p50_ms"]
    violations = []
    for tool, cur in sorted(stats.items()):
        if tool not in base:
            continue
        if tool in _FRAME_TIME_TOOLS:
            threshold = 3.0 * base[tool] + 2000.0
        else:
            threshold = max(3.0 * base[tool], base[tool] + 50.0)
        if cur > threshold:
            violations.append(
                f"{tool}: p50 {cur:.0f}ms > regression threshold "
                f"{threshold:.0f}ms (baseline {base[tool]:.0f}ms)"
            )
    return violations


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=["all", "a", "b"], default="all")
    ap.add_argument(
        "--update-baseline",
        action="store_true",
        help="R11: rewrite the committed latency baseline from this run's phase-B p50 stats",
    )
    args = ap.parse_args()
    _seed_analyze_fixture()
    _seed_ppsspp_mirror()
    report = asyncio.run(run(args.phase))
    stats = _ok_p50_by_tool(report["records"])
    report["latency_p50_by_tool"] = stats
    if args.update_baseline:
        BASELINE_PATH.write_text(
            json.dumps({"p50_ms": stats}, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"latency baseline updated: {BASELINE_PATH}")
    violations = _compare_latency_baseline(stats)
    report["latency_baseline_violations"] = violations
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    s = report["summary"]
    print("")
    print(
        f"=== {s['passed']} passed / {s['failed']} failed "
        f"(tools={report['meta'].get('tool_count')}, "
        f"handshake={report['meta'].get('handshake_ms')}ms) ==="
    )
    for f in s["failures"]:
        print(f"  FAIL {f['id']}: {f['status']} {f['error'][:150]} {f['problems']}")
    for v in violations:
        print(f"  LATENCY-REGRESSION {v}")
    print(f"report: {REPORT_PATH}")
    raise SystemExit(1 if (s["failed"] or violations) else 0)


if __name__ == "__main__":  # pragma: no cover
    main()
