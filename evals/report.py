"""Aggregate blind-eval JSONL runs into a markdown report (阶段 3 验收物).

Read-only over evals/runs/runs-*.jsonl (append-only; partially written
trailing lines are skipped). Report sections mirror the design doc §5.4:

1. 总览（variant × tier 成功率 + Wilson 95% CI）
2. 场景明细（n / pass / rate / 中位调用数 / 中位 tokens）
3. 混淆对（首工具不在 expected_first_tools 的 (expected, actual) 计数）
4. 门禁挂点分布（哪类门禁失败最多）
5. 错误恢复明细（出现过的错误码与是否按链恢复）
6. 效率（tool_calls / optimal_calls）

Usage (from the repository root/):
  <venv python> -m evals.report            # 写 reports/report-<ts>.md（临时名）
  <venv python> -m evals.report --stdout   # 同时打印到终端
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

_EVALS_DIR = Path(__file__).resolve().parent
_Z = 1.96


def _load_runs(runs_dir: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    if not runs_dir.is_dir():
        return runs
    for path in sorted(runs_dir.glob("runs-*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # partially written trailing line
    return runs


def _wilson(passes: int, n: int, z: float = _Z) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = passes / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom))


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _rate_str(passes: int, n: int) -> str:
    if n == 0:
        return "—"
    lo, hi = _wilson(passes, n)
    return f"{passes}/{n} ({_pct(passes / n)}, CI {_pct(lo)}–{_pct(hi)})"


def _median_str(values: list[float]) -> str:
    return f"{statistics.median(values):.1f}" if values else "—"


def build_report(runs: list[dict[str, Any]], scenarios_cfg: dict[str, Any]) -> str:
    meta = {s["id"]: s for s in scenarios_cfg["scenarios"]}
    L: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    L.append(f"# ppsspp-dfx 盲测报告（生成于 {now}）\n")
    if not runs:
        L.append("无运行记录（evals/runs/ 为空）。")
        return "\n".join(L)

    # 模型/变体/commit/工具面一览（多值时逐行列出——网格混版本说明锚点失效）
    commits = sorted({r.get("provenance", {}).get("git_commit", "?") for r in runs})
    models = sorted({r.get("model", "?") for r in runs})
    variants = sorted({r.get("variant", "?") for r in runs})
    surfaces = sorted({r.get("provenance", {}).get("tool_surface_sha256", "?")[:12] for r in runs})
    L.append(
        f"- 模型：{', '.join(models)}｜变体：{', '.join(variants)}｜commit：{', '.join(commits)}"
    )
    L.append(f"- 工具面哈希：{', '.join(surfaces)}（多值 = 网格跨越了工具面变更，需分层对比）")
    L.append(f"- 总运行数：{len(runs)}（基础设施失败的 run 不落盘，重跑即续）\n")

    # ── 1. 总览：variant × tier ──
    L.append("## 1. 总览（variant × tier）\n")
    L.append("| 变体 | 层 | 成功率 (Wilson 95% CI) |")
    L.append("|---|---|---|")
    by_vt: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in runs:
        sid = r.get("scenario_id", "?")
        by_vt[(r.get("variant", "?"), meta.get(sid, {}).get("tier", "?"))].append(r)
    for (variant, tier), rs in sorted(by_vt.items()):
        passes = sum(1 for r in rs if r.get("success"))
        L.append(f"| {variant} | {tier} | {_rate_str(passes, len(rs))} |")
    L.append("")

    # ── 2. 场景明细 ──
    L.append("## 2. 场景明细\n")
    L.append("| 场景 | 层 | 变体 | 成功率 | 中位调用数 (最优) | 中位 input tokens |")
    L.append("|---|---|---|---|---|---|")
    by_s: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in runs:
        by_s[(r.get("scenario_id", "?"), r.get("variant", "?"))].append(r)
    for (sid, variant), rs in sorted(by_s.items()):
        passes = sum(1 for r in rs if r.get("success"))
        calls = [float(len(r.get("tool_calls") or [])) for r in rs]
        toks = [float(r.get("usage", {}).get("input_tokens") or 0) for r in rs]
        optimal = meta.get(sid, {}).get("optimal_calls", "?")
        tier = meta.get(sid, {}).get("tier", "?")
        jp = " ⚠judge待评" if meta.get(sid, {}).get("judge_pending") else ""
        L.append(
            f"| {sid}{jp} | {tier} | {variant} | {_rate_str(passes, len(rs))} "
            f"| {_median_str(calls)} ({optimal}) | {_median_str(toks)} |"
        )
    L.append("")

    # ── 3. 混淆对（首工具选择失败）──
    L.append("## 3. 混淆对（首工具 ∈ expected_first_tools 之外）\n")
    confusion: Counter = Counter()
    for r in runs:
        sid = r.get("scenario_id", "?")
        expected = set(meta.get(sid, {}).get("expected_first_tools") or [])
        calls = r.get("tool_calls") or []
        if expected and calls:
            first = calls[0].get("name")
            if first not in expected:
                confusion[(sid, ", ".join(sorted(expected)), str(first))] += 1
    if not confusion:
        L.append("无——所有带首工具期望的 run 首选均命中。\n")
    else:
        L.append("| 场景 | 期望 | 实际首选 | 次数 |")
        L.append("|---|---|---|---|")
        for (sid, exp, act), n in confusion.most_common(10):
            L.append(f"| {sid} | {exp} | {act} | {n} |")
        L.append("")

    # ── 4. 门禁挂点分布 ──
    L.append("## 4. 门禁挂点分布\n")
    gate_fail: Counter = Counter()
    for r in runs:
        if r.get("success"):
            continue
        for g in r.get("gate_details") or []:
            if not g.get("pass"):
                gate_fail[(g.get("type"), r.get("scenario_id", "?"))] += 1
    if not gate_fail:
        L.append("无失败 run。\n")
    else:
        type_totals: Counter = Counter()
        for (gtype, _sid), n in gate_fail.items():
            type_totals[gtype] += n
        L.append("按门禁类型：" + "、".join(f"{g}×{n}" for g, n in type_totals.most_common()))
        L.append("")
        L.append("| 场景 | 失败门禁 | 次数 |")
        L.append("|---|---|---|")
        for (gtype, sid), n in sorted(gate_fail.items()):
            L.append(f"| {sid} | {gtype} | {n} |")
        L.append("")

    # ── 5. 错误恢复明细 ──
    L.append("## 5. 错误恢复明细（轨迹中出现过的错误码）\n")
    err_rows: list[str] = []
    for r in runs:
        calls = r.get("tool_calls") or []
        for i, c in enumerate(calls):
            code = c.get("error_code")
            if not code:
                continue
            # S16 (review v2): scenarios can carry one recovery gate per
            # expected error code; picking the FIRST one mislabeled every
            # other code's recovery. Match the gate whose detail names
            # the code, falling back to the single-gate shape.
            gates = r.get("gate_details") or []
            rec_gates = [g for g in gates if g["type"] == "recovery"]
            rec_gate = next(
                (g for g in rec_gates if code in str(g.get("detail", ""))),
                rec_gates[0] if len(rec_gates) == 1 else None,
            )
            rec = "—" if rec_gate is None else ("恢复✓" if rec_gate["pass"] else "恢复✗")
            err_rows.append(
                f"| {r.get('scenario_id')} | n={r.get('run_idx')} | #{i + 1} | {code} | {rec} |"
            )
    if not err_rows:
        L.append("全程未出现工具级错误。\n")
    else:
        L.append("| 场景 | run | 位置 | 错误码 | 恢复判定 |")
        L.append("|---|---|---|---|---|")
        L.extend(err_rows)
        L.append("")

    # ── 6. 效率（调用数/最优比）──
    L.append("## 6. 效率（中位 tool_calls ÷ optimal_calls）\n")
    L.append("| 场景 | 比值 | 说明 |")
    L.append("|---|---|---|")
    for (sid, variant), rs in sorted(by_s.items()):
        calls = [len(r.get("tool_calls") or []) for r in rs]
        optimal = meta.get(sid, {}).get("optimal_calls")
        if not optimal or not calls:
            continue
        ratio = statistics.median(calls) / max(1, optimal)
        note = "冗余调用" if ratio > 1.5 else ("贴最优" if ratio <= 1.0 else "")
        L.append(f"| {sid} ({variant}) | {ratio:.1f}× | {note} |")
    L.append("")

    # ── 7. 消融 delta（有多变体时）──
    if len(variants) > 1:
        L.append("## 7. 变体消融 delta（按场景配对）\n")
        L.append("| 场景 | " + " | ".join(variants) + " |")
        L.append("|---|" + "---|" * len(variants))
        for sid in sorted(meta):
            row = [sid]
            for v in variants:
                rs = by_s.get((sid, v), [])
                row.append(
                    _rate_str(sum(1 for r in rs if r.get("success")), len(rs)) if rs else "未跑"
                )
            L.append("| " + " | ".join(row) + " |")
        L.append("")

    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="aggregate blind-eval JSONL into markdown")
    ap.add_argument("--runs-dir", default=str(_EVALS_DIR / "runs"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()

    scenarios_cfg = yaml.safe_load((_EVALS_DIR / "scenarios.yaml").read_text(encoding="utf-8"))
    runs = _load_runs(Path(args.runs_dir))
    text = build_report(runs, scenarios_cfg)

    out = (
        Path(args.out)
        if args.out
        else (_EVALS_DIR / "reports" / f"report-{datetime.now():%Y%m%d-%H%M}.md")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    print(f"report → {out}")
    if args.stdout:
        print(text)


if __name__ == "__main__":
    main()
