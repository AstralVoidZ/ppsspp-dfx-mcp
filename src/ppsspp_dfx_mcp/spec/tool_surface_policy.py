"""spec/tool_surface_policy.py — 工具面治理登记表（schema / 破坏性提示策略）。

这些登记表是**契约**，不是工具层实现：守卫测试
（`tests/unit/l1_contract/test_annotation_policy.py`、
`tests/unit/l2_mcp_contract/test_output_schema_contract.py`）与守门脚本
（`scripts/report_schema_surface.py`，其退出码即 CI 信号）都从这里读取期望值。
放在 spec 层是为了让它们不再住在 tools 层，同时保持单一真相——`tools/_common.py`
仅作为 re-export 门面（历史导入点保持不变）。

These registries are imported by the guard tests and the gate script — a single
source of truth on purpose: if only one of them knew about an exemption, the
other would report a violation forever.
"""

from __future__ import annotations

__all__ = [
    "CONDITIONAL_REQUIRED_PARAMS",
    "DESTRUCTIVE_HINT_POLICY",
    "DESTRUCTIVE_TOOLS",
    "DYNAMIC_INPUT_PARAMETERS",
    "MULTI_SHAPE_OUTPUT_TOOLS",
    "NON_DESTRUCTIVE_BY_POLICY",
    "REMOVAL_ACTIONS",
]

# ── Schema governance: registered exceptions to `tool-schema-contract` ────
# Parameters whose shape is decided by *runtime data* and therefore cannot
# be constrained statically. Each entry is `<tool_name>.<param_name>` and
# MUST be argued for in the `tool-schema-contract` spec's exception clause
# before being listed here.
#
# Single source of truth on purpose: the guard test
# (`tests/unit/l2_mcp_contract/test_output_schema_contract.py`) and the
# gate script (`scripts/report_schema_surface.py`) both import this set —
# if only one of them knew about an exemption, the other would report a
# violation forever (the script's exit code is the CI signal).
DYNAMIC_INPUT_PARAMETERS: frozenset[str] = frozenset(
    {
        # Shape depends on the called script (each diagnostic script
        # declares its own Input model); callers must first learn the
        # target via ppsspp_list_scripts.
        "ppsspp_run_script.input",
    }
)

# ── Schema governance: tools whose return shape varies by branch ─────────
# The SDK validates every returned dict against the derived output contract
# (`func_metadata.convert_result`, see `spec/output_contract.py` docstring): a
# missing required field is a hard tool error. A tool that returns a
# *different view* per branch therefore cannot use a single-shape contract —
# its contract must be derived with `partial=True`.
#
# Each entry is `<tool_name>` -> why it has more than one shape. The guard
# test `test_multi_shape_tools_are_registered` walks every registered tool's
# source with AST and fails when a tool constructs ≥2 `*Response` classes but
# is absent here. Without that guard the failure mode is a runtime tool
# error on one branch only — invisible to a test suite that does not call
# that branch.
MULTI_SHAPE_OUTPUT_TOOLS: dict[str, str] = {
    "ppsspp_scan": (
        "三模式分发：pattern 返回匹配表，value 四相返回会话视图，"
        "strings 返回字符串表（partial=True 全字段可选）"
    ),
    "ppsspp_breakpoint": (
        "wait/trace 动作返回命中形状（{hit, already_paused, ...}/"
        "{hit, hits, bp_removed, resumed, ...}），管理动作返回"
        "{action, address, enabled, breakpoints[]}"
    ),
    "ppsspp_diff_memory": (
        "action 分发：snapshot/compare/drop/list 各返回不同视图（partial=True 全字段可选）"
    ),
    "ppsspp_batch_status": (
        "batch_id 省略（survey/list 模式）返回 BatchListResponse"
        "（{jobs, retention_jobs}），指定 batch_id 返回 BatchStatusResponse"
        "（{batch_id, status, ...}）— v0.1.6 合并 ppsspp_batch_list"
    ),
    "ppsspp_session": (
        "action='wait_ready' 返回 WaitReadyResponse（{action, ready, elapsed_s, "
        "probe_addr, probe_value, note}），其余 action 返回 SessionResponse"
    ),
    "ppsspp_batch_step": (
        "background=True 返回 BatchSubmitResponse（{action, batch_id, ...}），"
        "前台分支返回 BatchStepResponse（{executed, succeeded, results, ...}）"
    ),
    "ppsspp_frame_snapshot": (
        "无会话/不可暂停路径返回 StateObserverResponse，正常路径返回 FrameSnapshotResponse"
    ),
}

# ── Conditional-required parameters ─────────────────────────────────────
# A parameter declared `default: "0x0"` (or `0`) but *not* in the schema's
# `required` list is "omittable" to a generic JSON-Schema client, which will
# silently fill the default instead of failing. When that default is a
# sentinel rather than a real value, the call becomes a wrong call.
#
# The schema-level default cannot simply be dropped: callers that already
# omit the parameter rely on the current behaviour, and changing it would
# turn a silent wrong call into a hard break. So the compensation is at the
# description level: FR-007 requires the FIRST SENTENCE to state when the
# parameter is required.
#
# `tool.param` -> the actions that genuinely require it. The guard test
# `tests/unit/l1_contract/test_annotation_policy.py` asserts, for every entry
# here, that (a) the parameter exists, (b) it is NOT in the schema `required`
# list (else the entry is stale), (c) it HAS a non-null schema default (that
# is why the description has to compensate), and (d) its description's first
# sentence says "Required". Any drift fails the guard.
CONDITIONAL_REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "ppsspp_breakpoint.address": (
        "set",
        "remove",
        "update",
        "mem_set",
        "mem_remove",
        "mem_update",
    ),
    "ppsspp_query.address": ("func_remove", "func_scan"),
    # Every action reads from an address, so there is no action to exempt.
    "ppsspp_read_memory.address": ("<every action>",),
    "ppsspp_state_observer.address": ("register",),
    "ppsspp_step.address": ("run_until",),
    "ppsspp_replay.version": ("execute",),
    "ppsspp_replay.value": ("time_set",),
}

# ── Destructive-hint policy ─────────────────────────────────────────────
# The MCP `destructiveHint` annotation is what a client uses to decide
# whether a call needs extra confirmation, so an inconsistent hint is a
# safety signal pointing the wrong way. Before this registry existed the
# hints were applied ad hoc: `ppsspp_assemble` / `ppsspp_write_memory` /
# `ppsspp_write_register` were `true`, while `ppsspp_breakpoint` (which has
# `remove`) and `ppsspp_step` (which has `reset`) were `false` -- with no
# written rule anywhere saying why.
#
# The criterion below is the single source of truth. The guard test
# `tests/unit/l1_contract/test_annotation_policy.py` checks the live
# annotations against it, so a new tool cannot quietly pick the other
# convention.
DESTRUCTIVE_HINT_POLICY = (
    "A tool is destructive only if it IRREVERSIBLY CHANGES THE STATE OF THE "
    "EMULATED TARGET. Clearing the tool's own debug bookkeeping -- "
    "breakpoints, registered probes -- is NOT destructive: it touches no "
    "target state and is recoverable by re-registering."
)

#: Actions whose names suggest removal/reset. Used by the guard to find the
#: tools that owe an explanation under the policy above.
REMOVAL_ACTIONS: frozenset[str] = frozenset(
    {"remove", "mem_remove", "clear", "drop", "reset", "cancel", "abort", "delete"}
)

#: Tools marked destructive, with the reason under the policy.
DESTRUCTIVE_TOOLS: dict[str, str] = {
    "ppsspp_assemble": "writes instructions into the emulated address space",
    "ppsspp_write_memory": "writes bytes into the emulated address space",
    "ppsspp_write_register": "writes a CPU register of the emulated target",
}

#: Tools whose action names look destructive but are judged non-destructive.
#: Every entry needs a reason (guard rule G-4).
NON_DESTRUCTIVE_BY_POLICY: dict[str, str] = {
    "ppsspp_breakpoint": (
        "remove / mem_remove delete debugger bookkeeping, not target state; "
        "the tool's own docstring already notes size is part of the match key"
    ),
    "ppsspp_step": (
        "reset restarts the emulated run but changes no target memory; the "
        "debugger can pause/step immediately afterwards"
    ),
    "ppsspp_state_observer": (
        "clear removes registered probes (tool bookkeeping); no target state "
        "is touched. NB: the clear used to be permanent, which was an "
        "implementation defect, not a policy one"
    ),
    "ppsspp_replay": ("abort stops input playback; target memory is not written"),
    "ppsspp_diff_memory": (
        "drop discards a snapshot the tool itself captured; no target state "
        "is touched (found by the guard's action scan, not by inspection)"
    ),
    "ppsspp_batch_cancel": "cancels a queued batch job the tool itself created",
}
