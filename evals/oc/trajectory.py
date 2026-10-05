"""轨迹契约与 JSONL 落盘。

## 契约（本模块存在的全部理由）

`gates.evaluate` 只读 `tool_calls` + `final_answer`；`rescore.py` / `report.py`
读 record 顶层字段。**两条采集通道的 record 必须同构**，否则 LLM API 通道与
opencode 通道的 runs 无法合并对账（设计 D3）。

因此本模块把 record 构造集中为唯一入口，并让 `build_record` 的输出与
`evals/runner.py:run_scenario` 的 record 逐字段对齐：

| 字段 | 语义 | runner.py 对应 |
|---|---|---|
| `run_id` | `<前缀>-<ts>-<model_tag>-<variant>-<场景>-n<idx>` | 同构（前缀 `r` / `oc`） |
| `scenario_id` / `model` / `variant` / `run_idx` | 网格坐标 | 同 |
| `provenance` | 采集环境指纹 | runner 有 4 项哈希，oc 补齐等价信息 |
| `wall_ms` / `tool_calls` / `final_answer` / `usage` | 执行结果 | 同 |
| `stop_reason` | 终止原因 | 同（oc 细分更细，见下） |
| `gates` / `gate_details` / `success` | 门禁判定 | 同 |

**新增字段**（`collector` / `error_kind` / `pre_state` / `other_tool_names`）均为
**增量**：读取侧 `rescore.py` / `report.py` 按 key 取值，多余 key 不影响。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from evals.oc.log import get_logger

_log = get_logger("evals.oc.trajectory")

#: 网格坐标键——resume 去重与跨通道合并的锚点
DoneKey = tuple[str, str, str, int]


def run_id_for(
    prefix: str, timestamp: str, model: str, variant: str, scenario_id: str, run_idx: int
) -> str:
    """构造 run_id（`model` 取末段并截断，避免 provider 前缀撑爆标识）。"""
    model_tag = model.split("/")[-1][:24]
    return f"{prefix}-{timestamp}-{model_tag}-{variant}-{scenario_id}-n{run_idx}"


def gates_record(gates: list[dict[str, Any]]) -> dict[str, bool]:
    """type → pass；同类型门禁不得塌成一个 key（加 `#2` 后缀消歧）。

    复用 `evals.runner._gates_record` 保证两通道口径逐字一致；导入失败时
    退回等价本地实现（不因可选依赖阻断采集）。
    """
    try:
        from evals.runner import _gates_record as _impl
    except Exception as exc:  # noqa: BLE001
        _log.debug("复用 evals.runner._gates_record 失败，本地兜底: %s", exc)

        def _impl(gates: list[dict[str, Any]]) -> dict[str, bool]:  # type: ignore[misc]
            seen: dict[str, int] = {}
            out: dict[str, bool] = {}
            for g in gates:
                t = g["type"]
                seen[t] = seen.get(t, 0) + 1
                out[t if seen[t] == 1 else f"{t}#{seen[t]}"] = bool(g["pass"])
            return out

    return _impl(gates)


def build_record(
    *,
    run_id: str,
    scenario_id: str,
    model: str,
    variant: str,
    run_idx: int,
    provenance: dict[str, Any],
    wall_ms: int,
    tool_calls: list[dict[str, Any]],
    final_answer: str,
    usage: dict[str, Any],
    stop_reason: str,
    gates_result: dict[str, Any],
    collector: str = "opencode",
    error_kind: str | None = None,
    pre_state: dict[str, Any] | None = None,
    other_tool_names: Iterable[str] = (),
    stream_errors: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """构造与 runner.py 同构的轨迹 record（增量字段见模块 docstring）。"""
    return {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "model": model,
        "variant": variant,
        "run_idx": run_idx,
        "provenance": provenance,
        "started_at": None,  # 由调用方填 ISO 时间戳
        "wall_ms": wall_ms,
        "tool_calls": tool_calls,
        "final_answer": final_answer,
        "usage": usage,
        "stop_reason": stop_reason,
        "gates": gates_record(gates_result["gates"]),
        "gate_details": gates_result["gates"],
        "success": gates_result["success"],
        # ── oc 增量字段（读取侧按 key 取值，缺失无碍）──
        "collector": collector,
        "error_kind": error_kind,
        "pre_state": pre_state or {},
        "other_tool_names": list(other_tool_names),
        "stream_errors": list(stream_errors or []),
    }


def append_record(out_path: Path, record: dict[str, Any]) -> None:
    """追加一行 JSONL。`started_at` 未填时在此补齐。"""
    if not record.get("started_at"):
        from datetime import UTC, datetime

        record["started_at"] = datetime.now(UTC).isoformat()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def done_keys(out_path: Path) -> set[DoneKey]:
    """读取已有 JSONL 的网格坐标集合（断点续跑去重）。

    坏行跳过而非中断——一份 run 里出现一条截断行不应让整场续跑失败。
    """
    keys: set[DoneKey] = set()
    if not out_path.is_file():
        return keys
    for line in out_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            keys.add((r["scenario_id"], r["model"], r["variant"], int(r["run_idx"])))
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
            _log.debug("跳过无法解析的历史行: %s", exc)
    return keys
