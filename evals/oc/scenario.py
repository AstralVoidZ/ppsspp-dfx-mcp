"""场景卡访问层。

`scenarios.yaml` 是采集面的单一真源（runner.py / bridge.py 同源）。本模块只做
**访问与归一**，不改卡内容：`raw` 原样透传给 `gates.evaluate`，保证门禁口径
与 LLM API 通道逐字一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from evals.oc.errors import CollectorError, ErrorKind

DEFAULT_CONFIG = "scenarios.yaml"


@dataclass
class Scenario:
    """一张场景卡（`raw` 为原样 dict，直接喂门禁）。"""

    id: str
    tier: str
    prompt: str
    raw: dict[str, Any]
    max_turns: int = 12
    pre_state_sessions: int = 0
    mode: str = "fake"
    capabilities: list[str] = field(default_factory=list)

    @property
    def is_real(self) -> bool:
        return self.mode == "real"


def load_scenario_file(evals_dir: Path, name: str = DEFAULT_CONFIG) -> dict[str, Any]:
    path = Path(evals_dir) / name
    if not path.is_file():
        raise CollectorError(f"场景卡文件不存在: {path}", ErrorKind.EMPTY_OUTPUT)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "scenarios" not in data:
        raise CollectorError(f"场景卡文件结构异常（缺 scenarios）: {path}", ErrorKind.EMPTY_OUTPUT)
    return data


def fixtures_dir(evals_dir: Path, cfg: dict[str, Any]) -> Path:
    """卡内 `fixtures_dir` 相对 evals/ 解析——ground truth 的 fixture 根。"""
    return (Path(evals_dir) / cfg["fixtures_dir"]).resolve()


def load_scenarios(evals_dir: Path, name: str = DEFAULT_CONFIG) -> tuple[dict[str, Scenario], dict]:
    """加载全部场景卡；返回 (id → Scenario, 原始 yaml 顶层配置)。"""
    cfg = load_scenario_file(evals_dir, name)
    out: dict[str, Scenario] = {}
    for card in cfg["scenarios"]:
        pre = card.get("pre_state") or {}
        out[card["id"]] = Scenario(
            id=card["id"],
            tier=card.get("tier", ""),
            prompt=card["prompt"],
            raw=card,
            max_turns=int(card.get("max_turns", 12)),
            pre_state_sessions=int(pre.get("sessions", 0)),
            mode=card.get("mode", "fake"),
            capabilities=list(card.get("capabilities") or []),
        )
    return out, cfg


def resolve_ids(scenarios: dict[str, Scenario], spec: str) -> list[str]:
    """解析 `--scenarios` 规格：空 = 全部；否则逗号分隔并校验存在性。"""
    if not spec.strip():
        return list(scenarios)
    wanted = [s.strip() for s in spec.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in scenarios]
    if unknown:
        raise CollectorError(f"未知场景 id: {unknown}", ErrorKind.EMPTY_OUTPUT)
    return wanted
