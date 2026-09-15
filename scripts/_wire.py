"""Shared real-wire runner primitives (R-E, 2026-09-08).

Single source for the machinery both real-MCP runners need:

- scripts/verify_real_mcp.py (authoritative acceptance gate)
- scripts/probe_boundary_matrix.py (diagnostic boundary instrument)

v4 report gap G-5: the two runners used to duplicate the stdio launch
params, the boot sequence, the stale-session pre-clean and the MCP
error classification — and the duplicated classification promptly
diverged (McpError vs MCPError SDK spellings).
Import from here; do not re-implement.

Usage (both runners run from mcps/ppsspp-dfx-mcp):
    PYTHONPATH=src python scripts/<runner>.py
"""

from __future__ import annotations

import sys
import time
import os
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters
from mcp.client.stdio import get_default_environment

def find_package_root() -> Path:
    """Nearest ancestor (inclusive) containing pyproject.toml — the package
    root. Works both in the monorepo and in a standalone checkout."""
    here = Path(__file__).resolve().parent
    for cand in (here, *here.parents):
        if (cand / "pyproject.toml").is_file():
            return cand
    raise RuntimeError("package root not found (no pyproject.toml above scripts/)")


def find_workspace_root(package_root: Path) -> Path:
    """Root holding workspace assets (.venv / resource / .mcp.json).

    Resolution order: PPSSPP_DFX_WORKSPACE_ROOT env override, then the
    monorepo shape (package two levels below the workspace root), then the
    package root itself (standalone layout)."""
    env = os.environ.get("PPSSPP_DFX_WORKSPACE_ROOT")
    if env:
        return Path(env).resolve()
    for cand in (package_root.parent.parent, package_root.parent, package_root):
        if (cand / ".mcp.json").is_file():
            return cand
    return package_root


PACKAGE_ROOT = find_package_root()
WORKSPACE_ROOT = find_workspace_root(PACKAGE_ROOT)
SRC_DIR = str(PACKAGE_ROOT / "src")
ISO_PATH = os.environ.get("PPSSPP_DFX_TEST_ISO_PATH", "game.iso")

SESSION_TOOLS_ALL = {
    "ppsspp_read_memory", "ppsspp_write_memory", "ppsspp_get_pc",
    "ppsspp_query", "ppsspp_write_register", "ppsspp_evaluate",
    "ppsspp_disassemble", "ppsspp_search_disasm", "ppsspp_assemble",
    "ppsspp_breakpoint", "ppsspp_step", "ppsspp_state_observer",
    "ppsspp_batch_step", "ppsspp_wait_frames", "ppsspp_press_button",
    "ppsspp_hold_buttons", "ppsspp_send_analog", "ppsspp_screenshot",
    "ppsspp_dump_texture", "ppsspp_dump_clut", "ppsspp_gpu_stats",
    "ppsspp_gpu_record", "ppsspp_replay", "ppsspp_smoke_test",
    "ppsspp_run_script", "ppsspp_memory_info_search",
    "ppsspp_wait_breakpoint", "ppsspp_trace_memory_access",
    "ppsspp_frame_snapshot",
}

# Writable canary for the editable-install check (F-09).
_CANARY_MODULE = "ppsspp_dfx_mcp"


def _is_mcp_error(e: BaseException) -> bool:
    """mcp SDK exception spellings differ across versions (McpError /
    MCPError) — classify by name so protocol-level rejections land in
    rpc_error, not exception."""
    return "mcperror" in type(e).__name__.lower()


def check_editable_install_health() -> str | None:
    """The editable install's .pth may point at a stale repo path
    (e.g. after the repository was renamed or moved). Returns a human
    hint when the package is ONLY importable via the PYTHONPATH override
    (and never silently fixes it — the operator should re-install)."""
    try:
        import importlib.util
        spec = importlib.util.find_spec(_CANARY_MODULE)
    except Exception:  # noqa: BLE001 — diagnostics must not raise
        spec = None
    env_has_path = SRC_DIR in sys.path or any(
        p.rstrip("\\/") == SRC_DIR for p in sys.path)
    if spec is not None and not env_has_path:
        return None  # importable without our override — healthy
    if spec is not None:
        return None  # reachable via PYTHONPATH; callers set it anyway
    return (
        f"warning: '{_CANARY_MODULE}' is not importable — runners rely on "
        f"PYTHONPATH={SRC_DIR}; if 'pip show {_CANARY_MODULE}' exists, its "
        f"editable .pth points at a stale path (repo was renamed). Fix: "
        f"pip install -e mcps/ppsspp-dfx-mcp"
    )


def build_server_env() -> dict[str, str]:
    """Server child env — mirrors the project-level MCP client config plus the PYTHONPATH
    override (see check_editable_install_health)."""
    env = get_default_environment()
    env["PYTHONPATH"] = SRC_DIR
    env["PPSSPP_DFX_LOG_LEVEL"] = "INFO"
    hint = check_editable_install_health()
    if hint:
        print(hint)
    return env


def build_stdio_params(python_exe: str | None = None) -> StdioServerParameters:
    return StdioServerParameters(
        command=python_exe or sys.executable,
        args=["-m", _CANARY_MODULE],
        env=build_server_env(),
        cwd=str(WORKSPACE_ROOT),
    )


async def stop_all_sessions(session: Any) -> int:
    """Stop every session the server currently knows (F-06/R17): a
    leftover session holding the fixed PPSSPP port poisons the next
    start with PORT_CONFLICT. Returns the number stopped. Never
    raises — pre-clean is best-effort by design."""
    from mcp import ClientSession  # noqa: F401 — type documentation only

    stopped = 0
    try:
        r = await session.call_tool("ppsspp_session_list", {})
        s = (getattr(r, "structured_content", None)
             or getattr(r, "structuredContent", None) or {})
        for sess in s.get("sessions", []):
            sid = sess.get("session_id")
            if not sid:
                continue
            try:
                await session.call_tool(
                    "ppsspp_session", {"action": "stop", "session_id": sid})
                stopped += 1
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        print(f"  (pre-clean session_list failed: {type(e).__name__})")
    return stopped


async def call_tool(session: Any, tool: str, args: dict[str, Any],
                    *, session_id: str | None = None,
                    timeout_s: float = 120.0,
                    session_tools: set[str] = SESSION_TOOLS_ALL,
                    ) -> dict[str, Any]:
    """Single tool call returning a plain record dict:
    {status, latency_ms, structured, text, error}. status ∈
    ok | tool_error | rpc_error | exception | timeout. session_id is
    auto-injected for session-scoped tools; the literal
    __SESSION_ID__ is substituted everywhere."""
    if tool in session_tools and "session_id" not in args:
        args = {**args, "session_id": session_id or "sess_unknown_guard"}
    sid = session_id or ""
    args = {k: (sid if v == "__SESSION_ID__" else v)
            for k, v in args.items()}
    import asyncio
    t0 = time.perf_counter()
    rec: dict[str, Any] = {"structured": None, "text": "", "error": ""}
    try:
        r = await asyncio.wait_for(session.call_tool(tool, args),
                                   timeout=timeout_s)
        rec["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        texts = [c.text for c in (getattr(r, "content", None) or [])
                 if getattr(c, "type", None) == "text"]
        rec["text"] = "\n".join(texts)[:1200]
        rec["structured"] = (getattr(r, "structured_content", None)
                             or getattr(r, "structuredContent", None))
        err = getattr(r, "is_error", None)
        if err is None:
            err = getattr(r, "isError", False)
        rec["status"] = "tool_error" if err else "ok"
        if err:
            rec["error"] = rec["text"]
    except asyncio.TimeoutError:
        rec["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        rec["status"] = "timeout"
        rec["error"] = f"exceeded {timeout_s}s"
    except Exception as e:  # noqa: BLE001
        rec["latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        rec["status"] = "rpc_error" if _is_mcp_error(e) else "exception"
        rec["error"] = f"{type(e).__name__}: {e}"[:400]
    return rec


async def boot_session(session: Any, *, iso_path: str = ISO_PATH,
                       resilient: bool = True, wait_ready_s: float = 100.0,
                       settle_s: float = 12.0,
                       ) -> tuple[bool, str, dict[str, Any]]:
    """Pre-clean → resilient start → wait_ready → title-screen settle.

    Returns (ok, message, structured-from-wait_ready). A healthy boot
    is seconds; the settle window lets the title screen reach its
    steady loop so address probes (game_mode) are meaningful."""
    stale = await stop_all_sessions(session)
    if stale:
        print(f"  [pre-clean] stopped {stale} stale session(s)")
    r = await call_tool(session, "ppsspp_session",
                        {"action": "start", "iso_path": iso_path,
                         "resilient": resilient})
    if r["status"] != "ok":
        return False, f"start failed: {r['error'][:200]}", None
    sid = (r.get("structured") or {}).get("session_id")
    if not sid:
        return False, "start returned no session_id", None
    r_ready = await call_tool(session, "ppsspp_session",
                              {"action": "wait_ready", "session_id": sid,
                               "timeout_s": wait_ready_s})
    if r_ready["status"] != "ok":
        return False, f"wait_ready failed: {r_ready['error'][:200]}", (
            r_ready.get("structured"))
    if settle_s:
        time.sleep(settle_s)
    return True, f"booted {sid}", r_ready.get("structured")


def liveness_three_checks(smoke_structured: dict[str, Any] | None,
                          ) -> tuple[bool, dict[str, bool]]:
    """R-C three-check liveness judge (F-07): iso_loaded / cpu_running /
    ws_connected must all pass. game_mode_valid is game-phase dependent
    (title-screen attract mode flips it after minutes) and is
    deliberately EXCLUDED — a smoke `overall_status=fail` alone is not
    a liveness signal. Returns (alive, per-check dict)."""
    s = smoke_structured or {}
    checks = {c.get("name"): bool(c.get("passed"))
              for c in s.get("checks", []) if isinstance(c, dict)}
    alive = all(checks.get(k) for k in
                ("iso_loaded", "cpu_running", "ws_connected"))
    return alive, checks
