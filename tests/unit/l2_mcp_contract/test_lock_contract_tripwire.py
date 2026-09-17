"""R16 tripwire: the W1 per-session lock contract is a documented set.

The lock semantics live in ``SessionManager.session_lock``'s docstring.
This test enforces them mechanically: every registered MCP tool whose
body opens ``session_client`` / ``session_capture`` MUST be in the
HOLDING set below; any tool that opens neither MUST NOT silently appear
there. A new tool that changes the picture forces a conscious update of
this file (and of the docstring), keeping the contract from drifting —
the same failure mode the W1 double-clamp bug came from.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TESTS_LOCKED_TOOLS = Path(__file__).resolve().parents[1] / ".."
SRC_TOOLS = (Path(__file__).resolve().parents[3] / "src" / "ppsspp_dfx_mcp" / "tools").resolve()

# R16 contract set — keep in sync with SessionManager.session_lock docstring.
HOLDING_LOCK: frozenset[str] = frozenset(
    {
        # session_client users
        "ppsspp_read_memory",
        "ppsspp_write_memory",
        "ppsspp_disassemble",
        "ppsspp_query",
        "ppsspp_get_pc",
        "ppsspp_write_register",
        "ppsspp_evaluate",
        "ppsspp_search_disasm",
        "ppsspp_assemble",
        "ppsspp_breakpoint",
        "ppsspp_step",
        "ppsspp_smoke_test",
        "ppsspp_state_observer",
        "ppsspp_batch_step",
        "ppsspp_replay",
        "ppsspp_gpu_stats",
        "ppsspp_gpu_record",
        "ppsspp_memory_info_search",
        "ppsspp_memory_map",
        # session_capture users
        "ppsspp_screenshot",
        "ppsspp_dump_texture",
        "ppsspp_dump_clut",
        # hold_buttons / press_button / send_analog use session_client via input.py
        "ppsspp_press_button",
        "ppsspp_hold_buttons",
        "ppsspp_send_analog",
        # P4 frame_snapshot: pause→capture→resume wraps the WHOLE body in one
        # session context (unlike wait_breakpoint/trace — no lock-free wait).
        "ppsspp_frame_snapshot",
    }
)

# H1 (2026-09-07): lock-free WAIT, locked sub-operations. These tools open
# session contexts (arm / probe / capture / cleanup) but the breakpoint
# WAIT itself subscribes to the observer's cpu.stepping fan-out and holds
# NO lock — concurrent reads must keep working during the wait (verified
# by test_workflows_tools.py). All CPU-state mutations stay inside their
# locked regions.
PARTIAL_HOLD_LOCK: frozenset[str] = frozenset(
    {
        "ppsspp_wait_breakpoint",
        "ppsspp_trace_memory_access",
    }
)

NOT_HOLDING_LOCK: frozenset[str] = frozenset(
    {
        # run_script does NOT open a session context in its own body —
        # diagnostic scripts access the session themselves via
        # ctx.session_id, outside this lock. Documented contract (R16).
        "ppsspp_run_script",  # executes manifest scripts (ctx-scoped)
        "ppsspp_wait_frames",  # pure wall-clock sleep + liveness checks
        "ppsspp_analyze_log",  # file only
        "ppsspp_convert_address",  # pure arithmetic
        "ppsspp_health",  # server liveness
        "ppsspp_session",  # lifecycle (manages the lock owner itself);
        # wait_ready polls the raw session transport
        # (lock-free, like wait_frames)
        "ppsspp_session_list",  # read-only listing
        "ppsspp_list_scripts",  # manifest read
        "ppsspp_reload_scripts",  # manifest reload
        "ppsspp_list_addresses",  # yaml read
        # A1 (2026-09-10): lock-free by contract — polling/cancelling a
        # background batch MUST stay responsive while that batch holds the
        # session lock for its whole (possibly minutes-long) duration.
        "ppsspp_batch_status",  # registry read (no WS, no lock)
        "ppsspp_batch_cancel",  # registry task.cancel (no WS, no lock)
        "ppsspp_batch_list",  # registry read (no WS, no lock)
    }
)

_SESSION_OPENERS = {
    "session_client",
    "session_capture",
    "session_client_with_transport",
}

# Delegated openers: helper coroutines (same module) that a tool body
# calls instead of opening the context inline. batch_step delegates its
# whole locked body to _execute_batch (A1 refactor) — the lock contract
# is unchanged, only the call is one level deeper.
_DELEGATE_OPENERS = {
    "_execute_batch",
}


def _tool_functions() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """AST-extract every @mcp.tool-decorated function in tools/*.py,
    keyed by REGISTERED tool name (decorator ``name=`` kwarg when
    present, else the function name)."""
    out: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for py in sorted(SRC_TOOLS.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                fn = dec.func if isinstance(dec, ast.Call) else dec
                if not (isinstance(fn, ast.Attribute) and fn.attr == "tool"):
                    continue
                registered = node.name
                if isinstance(dec, ast.Call):
                    for kw in dec.keywords:
                        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                            registered = str(kw.value.value)
                            break
                out[registered] = node
    return out


def _calls_session_opener(fn) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _SESSION_OPENERS:
                return True
            if node.func.id in _DELEGATE_OPENERS:
                return True
    return False


def test_every_registered_tool_is_classified():
    tools = _tool_functions()
    classified = HOLDING_LOCK | NOT_HOLDING_LOCK | PARTIAL_HOLD_LOCK
    unclassified = sorted(set(tools) - classified)
    unknown = sorted(classified - set(tools))
    assert not unclassified, (
        f"tools not classified in the lock contract: {unclassified} — "
        f"decide HOLDING vs PARTIAL_HOLD vs NOT_HOLDING and update the "
        f"sets + the SessionManager.session_lock docstring"
    )
    assert not unknown, f"contract lists non-existent tools: {unknown}"


def test_lock_holders_actually_open_a_session_context():
    tools = _tool_functions()
    liars = sorted(
        name for name in HOLDING_LOCK if name in tools and not _calls_session_opener(tools[name])
    )
    assert not liars, (
        f"{liars} claim to hold the session lock but never open "
        f"session_client/session_capture — reclassify them"
    )


def test_partial_hold_tools_open_a_session_context():
    """PARTIAL_HOLD members really do run locked sub-operations (the
    classification is meaningless for a tool that never opens one)."""
    tools = _tool_functions()
    liars = sorted(
        name
        for name in PARTIAL_HOLD_LOCK
        if name in tools and not _calls_session_opener(tools[name])
    )
    assert not liars, (
        f"{liars} claim PARTIAL_HOLD (locked sub-ops + lock-free wait) "
        f"but never open session_client/session_capture — they are "
        f"plain NOT_HOLDING; reclassify them"
    )


def test_non_holders_do_not_open_a_session_context():
    tools = _tool_functions()
    violators = sorted(
        name for name in NOT_HOLDING_LOCK if name in tools and _calls_session_opener(tools[name])
    )
    assert not violators, (
        f"{violators} open a session context but are documented as "
        f"NOT holding the per-session lock — they WILL race the "
        f"single-consumer transport; move them to HOLDING_LOCK (and "
        f"accept the serialization) or restructure"
    )
