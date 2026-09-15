"""P6 baseline: the committed tools/list surface must not drift silently.

Anchor: scripts/dump_tool_surface.py exports the full registered surface
(name/description/annotations) to tool_surface_baseline.json. Any change
to a tool description or annotations makes these tests fail — regenerate
the baseline WITH THE SAME COMMIT as the change and review the diff
(never as a drive-by). This is the drift guard the P6 description
slimming depends on: without it, trimmings and edits are unreviewable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from ppsspp_dfx_mcp import server as server_mod

BASELINE_PATH = Path(__file__).resolve().parent / "tool_surface_baseline.json"


def _canonical(schema: dict | None) -> str:
    """Must match `dump_tool_surface._canonical` — hashes are compared."""
    return json.dumps(schema or {}, ensure_ascii=False, sort_keys=True)


@pytest.fixture(scope="module")
def live() -> dict[str, dict]:
    server_mod.register_all_tools()
    tools = asyncio.run(server_mod.mcp.list_tools())
    out: dict[str, dict] = {}
    for t in tools:
        ann = getattr(t, "annotations", None)
        in_json = _canonical(t.input_schema)
        out_json = _canonical(t.output_schema)
        out[t.name] = {
            "description": t.description or "",
            "input_schema_sha1": hashlib.sha1(
                in_json.encode("utf-8")).hexdigest(),
            "input_schema_chars": len(in_json),
            "input_schema_properties": sorted(
                (t.input_schema or {}).get("properties") or {}
            ),
            "output_schema_sha1": hashlib.sha1(
                out_json.encode("utf-8")).hexdigest(),
            "annotations": {} if ann is None else {
                "read_only_hint": getattr(ann, "read_only_hint", None),
                "destructive_hint": getattr(ann, "destructive_hint", None),
                "idempotent_hint": getattr(ann, "idempotent_hint", None),
                "open_world_hint": getattr(ann, "open_world_hint", None),
            },
        }
    return out


def _committed() -> dict[str, dict]:
    assert BASELINE_PATH.is_file(), (
        f"baseline missing: {BASELINE_PATH} — regenerate with "
        f"PYTHONPATH=src python scripts/dump_tool_surface.py"
    )
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["tools"]


def test_no_tool_added_or_removed(live):
    baseline = _committed()
    added = sorted(set(live) - set(baseline))
    removed = sorted(set(baseline) - set(live))
    assert not added and not removed, (
        f"tool surface drifted (added={added}, removed={removed}) — "
        f"regenerate the baseline via scripts/dump_tool_surface.py and "
        f"review the diff in the same commit"
    )


def test_descriptions_match_baseline(live):
    baseline = _committed()
    drifted = [
        name for name, entry in baseline.items()
        if name in live
        and live[name]["description"] != entry["description"]
    ]
    assert not drifted, (
        f"description drift on {drifted} — if intended, regenerate the "
        f"baseline via scripts/dump_tool_surface.py and include the diff "
        f"in your commit"
    )


def test_annotations_match_baseline(live):
    baseline = _committed()
    drifted = [
        name for name, entry in baseline.items()
        if name in live and live[name]["annotations"] != entry["annotations"]
    ]
    assert not drifted, (
        f"annotation drift on {drifted} — regenerate the baseline via "
        f"scripts/dump_tool_surface.py and include the diff in your commit"
    )


def test_input_parameter_names_match_baseline(live):
    """参数**名集合**不得变化。

    单列一条断言（而非只靠 hash）：改名时失败信息直接给出新/旧名字，而不是
    两个不透明的 digest。openspec change
    `ppsspp-dfx-mcp-protocol-and-schema` 的硬约束是「schema 治理是**追加契约**
    非重构」——本测试即该约束的可执行形式。
    """
    baseline = _committed()
    drifted = {}
    for name, entry in baseline.items():
        if name not in live:
            continue
        before = set(entry.get("input_schema_properties") or [])
        after = set(live[name]["input_schema_properties"])
        if before != after:
            drifted[name] = {
                "added": sorted(after - before),
                "removed": sorted(before - after),
            }
    assert not drifted, (
        f"参数名集合漂移：{json.dumps(drifted, ensure_ascii=False)} — "
        f"本变更不得改参数名；若确为有意，same-commit 重新生成基线并说明"
    )


def test_input_schemas_match_baseline(live):
    """参数**类型/约束**不得变化（整份 inputSchema 的 hash 比对）。"""
    baseline = _committed()
    drifted = sorted(
        name for name, entry in baseline.items()
        if name in live
        and live[name]["input_schema_sha1"] != entry.get("input_schema_sha1")
    )
    assert not drifted, (
        f"inputSchema 漂移 on {drifted} — 参数类型或约束被改动；若确为有意，"
        f"same-commit 重新生成基线"
    )


def test_output_schemas_match_baseline(live):
    """outputSchema 的变更必须是有意的、经过 review 的。

    本变更的核心交付就是 outputSchema 治理（34 个工具由「无契约」转为结构化），
    故基线必须能看见 outputSchema —— 否则治理成果与后续漂移都无从察觉。
    """
    baseline = _committed()
    drifted = sorted(
        name for name, entry in baseline.items()
        if name in live
        and live[name]["output_schema_sha1"] != entry.get("output_schema_sha1")
    )
    assert not drifted, (
        f"outputSchema 漂移 on {drifted} — 若为有意的契约变更，same-commit "
        f"重新生成基线并 review diff"
    )
