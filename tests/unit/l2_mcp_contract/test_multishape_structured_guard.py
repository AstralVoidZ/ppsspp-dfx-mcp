"""🔴-1 guard: multi-shape tools must keep every branch's keys in
structuredContent.

SDK 2.x convert_result re-serializes the returned dict against the
output contract — keys not declared in the contract are SILENTLY
STRIPPED from structuredContent (validation "passes", data is gone).
Partial contracts used to hide this: validation passed while branch
keys vanished. This guard runs each multi-shape tool's real
func_metadata.convert_result over a sample dict per branch and asserts
the serialized output preserves the branch's keys.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.utilities.func_metadata import func_metadata

from ppsspp_dfx_mcp.tools import (
    batch_step,
    breakpoint,
    diff,
    scan,
    session,
)

BRANCH_SAMPLES: dict[tuple[object, str], dict] = {
    # tool function, branch label -> sample return dict
    (session.session, "start/get"): {
        "session_id": "s", "iso_path": "i", "pid": 1,
        "ws_url": "ws://x", "created_at": "t", "last_active_at": "t",
        "exec_count": 0, "ws_connected": True, "recovered": 0,
        "restored": 0, "ppsspp_version": {},
    },
    (session.session, "wait_ready"): {
        "action": "wait_ready", "ready": True, "elapsed_s": 0.0,
        "probe_addr": "0x08804000", "probe_value": "0x0", "note": None,
    },
    (session.session, "list"): {"sessions": [], "count": 0},
    (breakpoint.breakpoint, "set/get"): {
        "action": "set", "address": 1, "enabled": True, "breakpoints": [],
    },
    (breakpoint.breakpoint, "wait"): {
        "hit": True, "already_paused": False, "timeout_s": 1.0,
        "pc": "0x0", "reason": None, "related_address": None, "ticks": None,
    },
    (breakpoint.breakpoint, "stats"): {
        "mode": "stats", "window_s": 30.0, "total_hits": 0, "by_pc": [],
    },
    (breakpoint.breakpoint, "trace"): {
        "hit": True, "address": 1, "access": "r", "timeout_s": 1.0,
        "hits": [], "bp_removed": True, "resumed": True, "note": None,
    },
    (diff.diff_memory, "snapshot"): {
        "handle": "h", "start": "0x0", "size_bytes": 1, "checksum": "c",
    },
    (diff.diff_memory, "compare"): {
        "handle": "h", "start": "0x0", "size_bytes": 1,
        "changed_count": 0, "truncated": False, "changes": [],
    },
    (diff.diff_memory, "list"): {"handles": [], "count": 0, "max_snapshots": 4},
    (diff.diff_memory, "drop"): {"handle": "h", "dropped": True},
    (batch_step.batch_step, "foreground"): {
        "action": "run", "total": 1, "executed": 1, "succeeded": 1,
        "failed": 0, "skipped": 0, "recording_mode": False,
        "results": [{"index": 0, "type": "wait", "status": "success"}],
        "aborted": False,
    },
    (batch_step.batch_step, "background submit"): {
        "action": "submitted", "batch_id": "b", "session_id": "s",
        "total": 1, "estimated_s": 1.0, "hint": "poll",
    },
    (batch_step.batch_status, "status single"): {
        "batch_id": "b", "session_id": "s", "status": "completed",
        "executed": 1, "total": 1, "error": None, "result": {},
    },
    (batch_step.batch_status, "list survey"): {"jobs": [], "retention_jobs": 32},
    (scan.scan, "pattern"): {
        "mode": "pattern", "action": "scan", "address": "0x0",
        "value": [], "size": 0, "count": 0,
    },
    (scan.scan, "background submit"): {
        "action": "submitted", "batch_id": "b", "session_id": "s",
        "estimated_s": 1.0, "hint": "poll",
    },
}


@pytest.mark.parametrize(("tool_fn", "branch"), sorted(BRANCH_SAMPLES, key=lambda k: k[1]))
def test_branch_keys_survive_structured_content(tool_fn, branch):
    label = branch
    sample = BRANCH_SAMPLES[(tool_fn, branch)]
    meta = func_metadata(tool_fn)
    converted = meta.convert_result(sample)
    sc = converted.structured_content if hasattr(converted, "structured_content") else converted
    assert isinstance(sc, dict), f"{label}: structuredContent lost ({type(sc).__name__})"
    missing = set(sample) - set(sc)
    assert not missing, (
        f"{label}: keys stripped from structuredContent by the output "
        f"contract (🔴-1 regression): {sorted(missing)}"
    )
