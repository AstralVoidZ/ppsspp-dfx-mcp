"""opencode 采集线编排 + CLI。

单 run 流程（每层一个模块，可单测）：

```
scenario.py   场景卡 + fixtures 根
   │
env.py        ① 构造 server env（fake/real 单一真源）
              ② XDG 隔离（每 run 唯一，拷入 opencode profile 保认证）
              ③ 会话预置（fake 模式，短命 server 落盘 → opencode server 恢复）
   │
attach.py     ④ 常驻 opencode server（ensure 复用 / 拉起；URL 白名单）
   │
runner.py     ⑤ opencode run（flag 能力探测 + 超时安全 + 锁竞争重试）
   │
events.py     ⑥ 事件流解析（工具名归一 + usage 聚合 + 诊断通道）
   │
trajectory.py ⑦ record 构造（与 runner.py 同构）+ JSONL 落盘
   │
gates.py      ⑧ 门禁评分（与 LLM API 通道同一份实现）
```

## variant 语义（本通道的固有限制）

`runner.py` 的 B0/B1/B2 消融靠**运行时拼接 system prompt**；opencode 通道的
prompt 由 `.opencode/agents/ppsspp-dfx-collector.md` 静态定义，采集器无法注入或
剥离。因此本通道**只有 B1 是有意义的**：非 B1 的 `--variant` 仅作标签兼容，
结果与 LLM API 通道的同名变体**不可比**。命令行会显式告警，避免产出误导性
对比数据。
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals.oc import progress
from evals.oc.attach import ServeManager, validate_attach_url
from evals.oc.env import build_server_env, isolated_xdg_env, resolve_iso, seed_sessions
from evals.oc.errors import CollectorError, ErrorKind
from evals.oc.events import classify_stream_error, parse_stream
from evals.oc.log import get_logger
from evals.oc.runner import CliRunner, RunOptions
from evals.oc.scenario import Scenario, fixtures_dir, load_scenarios, resolve_ids
from evals.oc.trajectory import append_record, build_record, done_keys, run_id_for

_log = get_logger("evals.oc.collect")

_EVALS_DIR = Path(__file__).resolve().parent.parent
#: 仓库根——**不假定存在父目录**（独立 checkout 场景，见 runner.py W17）
_PKG_ROOT = _EVALS_DIR.parent
_SRC_ROOT = _PKG_ROOT / "src"
_TESTS_ROOT = _PKG_ROOT / "tests"

DEFAULT_MODEL = "opencode/mimo-v2.6-flash-free"
DEFAULT_AGENT = "ppsspp-dfx-collector"
DEFAULT_TIMEOUT_S = 600

SYSTEM_TEMPLATE = (
    "你是一个使用 MCP 工具的 PSP 模拟器调试助手。\n"
    "请根据任务需要选择并调用可用的工具；完成任务后，用中文给出最终答案。\n"
)


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dir_sha256(path: Path) -> str:
    if not path.is_dir():
        return "missing"
    h = hashlib.sha256()
    for f in sorted(path.glob("*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PKG_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        _log.debug("git rev-parse 失败: %s", exc)
        return "unknown"


def resolve_work_dir(explicit: str = "") -> Path:
    """定位 opencode 的工作目录——即**持有 `.opencode/` 的那一层**。

    opencode 从 cwd 读 `.opencode/agents/` 与 `.opencode/mcp.json`，因此 cwd 必须是
    配置所在目录。本仓库里 `.opencode/` 在**仓库根**而非 MCP 子项目
    （`mcps/ppsspp-dfx-mcp/.opencode/` 不存在）——旧实现用
    `_PKG_ROOT.parents[1]` 硬取仓库根，在 monorepo 下碰巧正确，但在 MCP 包
    **独立 checkout** 时会指到包外不存在的路径（W17 同类问题的另一面）。

    故改为从包根向上探测首个含 `.opencode/agents/` 的目录，都找不到则回落包根
    并告警（此时 `--agent` 的可发现性校验会给出明确失败原因）。
    """
    if explicit:
        return Path(explicit).resolve()
    for candidate in [_PKG_ROOT, *_PKG_ROOT.parents]:
        if (candidate / ".opencode" / "agents").is_dir():
            return candidate
    _log.warning("未找到含 .opencode/agents 的目录，回落 %s；请用 --work-dir 指定", _PKG_ROOT)
    return _PKG_ROOT


def build_provenance(agent_path: Path | None, fixtures: Path) -> dict[str, Any]:
    """采集环境指纹（对齐 runner.py 的四项 + oc 专有项）。"""
    baseline = _TESTS_ROOT / "unit" / "l2_mcp_contract" / "tool_surface_baseline.json"
    return {
        "git_commit": _git_commit(),
        "tool_surface_sha256": _sha256_file(baseline),
        # 通道差异：oc 通道的 "instructions" 是 agent 定义文件而非 server
        # instructions，故指纹取 agent 文件内容——同键不同源，文档已注明。
        "instructions_sha256": _sha256_file(agent_path) if agent_path else "missing",
        "fixture_dir_sha256": _dir_sha256(fixtures),
        "collector": "opencode",
        "agent_sha256": _sha256_file(agent_path) if agent_path else "missing",
    }


def build_prompt(scenario: Scenario, real_mode: bool, *, variant: str) -> str:
    """构造送给 opencode 的 prompt（system 模板 + 场景卡，替换 `{{REAL_ISO}}`）。"""
    body = scenario.prompt.replace("{{REAL_ISO}}", resolve_iso(real_mode))
    return f"{SYSTEM_TEMPLATE}\n\n{body}"


def _stop_reason(parsed_text: str, tool_count: int, run_ok: bool) -> str:
    """终止原因。

    旧实现写死 `"final_answer" if final_answer else "max_turns"`，把「压根没
    产出任何输出」误报成「撞转数上限」——正是 `runs-oc-*.jsonl` 里两条
    `calls=0` 记录显示 `stop=max_turns` 的原因，读起来像模型跑了 12 轮没答对，
    实际是一轮都没跑起来。此处按事实细分。
    """
    if not run_ok:
        return "driver_failure"
    if parsed_text:
        return "final_answer"
    if tool_count:
        return "no_final_text"
    return "empty_stream"


def check_mcp_registered(
    work_dir: Path,
    server_key: str = "ppsspp-dfx",
    env: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """预检：opencode 是否真的注册了 ppsspp-dfx MCP server。

    ## 为什么必须有这个检查

    opencode v2.0.19 的配置源是 `<cwd>/.opencode/` 与 `~/.config/opencode/`，
    读的是 **`opencode.json` 的 `mcp.servers`**（`opencode debug config` 实测）。
    仓库里原有的 `.opencode/mcp.json` 是 Claude-Code / MCP-Inspector 风格的配置，
    **opencode 根本不读它**——`opencode mcp list` 只会回 `No MCP servers configured`。

    后果是：agent 拿不到任何 ppsspp 工具，只能退而用 opencode 自带的
    `execute` / `shell` / `skill`，轨迹里 `tool_calls=0`、门禁全挂，读起来像
    「模型不会用工具」，实际是**接线缺失**。这是本采集线最贵的一类隐性故障，
    必须在开跑前 3 秒内暴露。

    ## 为什么 `env` 是必传参数（2026-09-30 实测事故）

    预检必须跑在**与真实 run 完全相同的环境**里。曾经这里隐式继承
    `os.environ`，而 runner 跑在 `isolated_xdg_env()` 产出的隔离环境里——
    预检说 `ppsspp-dfx connected` 放行，run 里却 `Unknown tool
    'ppsspp-dfx.ppsspp_session'`：**预检验证的不是实际运行环境，等于没验证**。
    环境差异最大的两个来源是 XDG 隔离树与 opencode 后台 service 的握手端点，
    二者都会在预检与实际 run 之间制造「预检通过、实跑 Unknown tool」的假阳性。

    Returns:
        (是否就绪, 人读诊断)。
    """
    bin_path = ServeManager().bin_path
    try:
        proc = subprocess.run(
            [bin_path, "mcp", "list"],
            cwd=str(work_dir),
            capture_output=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"opencode mcp list 探测失败: {exc}"
    blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if server_key not in blob:
        return (
            False,
            f"opencode 未注册 MCP server {server_key!r}（mcp list 输出: "
            f"{blob.strip()[:120] or '(空)'}）\n"
            f"  修复（opencode v2 读 opencode.json 的 mcp.servers，不读 .opencode/mcp.json）：\n"
            f"    opencode mcp add {server_key} -- <venv python> -m ppsspp_dfx_mcp\n"
            f"  或加 --skip-mcp-check 跳过本预检。",
        )
    if "connected" not in blob:
        return (
            False,
            f"MCP server {server_key!r} 已登记但未 connected（mcp list: {blob.strip()[:160]}）。\n"
            f"  先手动跑一次 `opencode mcp list` 排查子进程能否启动。",
        )
    if "background service" in blob:
        return (
            False,
            f"opencode 后台 service 起不来，CLI 无法加载任何 MCP server"
            f"（mcp list: {blob.strip()[:160]}）。\n"
            f"  常见成因：隔离了 XDG_STATE_HOME/CACHE_HOME 而 service 握手端点在 STATE 树里——"
            f"XDG 隔离只允许重定向 DATA/CONFIG。",
        )
    return True, f"{server_key} connected"


class Collector:
    """一次采集会话的编排器（持有常驻 server 与 runner）。"""

    def __init__(
        self,
        *,
        scenarios: dict[str, Scenario],
        fixtures: Path,
        out_path: Path,
        model: str,
        agent: str,
        variant: str,
        work_dir: Path,
        server_url: str,
        server_password: str | None,
        timeout_s: int,
        real_mode: bool = False,
        seed: bool = True,
        dump_events: bool = False,
        real_settle_s: float = 8.0,
        resume: bool = True,
    ) -> None:
        self.scenarios = scenarios
        self.fixtures = fixtures
        self.out_path = out_path
        self.model = model
        self.agent = agent
        self.variant = variant
        self.work_dir = work_dir
        self.real_mode = real_mode
        self.seed_enabled = seed
        self.dump_events = dump_events
        self.real_settle_s = real_settle_s
        self.done = done_keys(out_path) if resume else set()
        self._ts = datetime.now(UTC)
        agent_path = (work_dir / ".opencode" / "agents" / f"{agent}.md") if agent else None
        self.provenance = build_provenance(agent_path, fixtures)
        self.runner = CliRunner(
            model=model,
            agent=agent,
            work_dir=str(work_dir),
            server_url=server_url,
            server_password=server_password,
            timeout_s=timeout_s,
            # `.opencode/mcp.json`（本机配置，gitignored）里写死了某台机器的
            # 绝对路径 `PPSSPP_DFX_PROJECT_ROOT`。`config.project_root()` 的取值
            # 顺序是 env > cwd，且 env 指向不存在目录时**直接抛 ConfigInvalid**
            # （不回落 cwd）——仓库一旦移动/换机，整个 server 起不来。这里按探测
            # 出的 work_dir 主动注入正确值，消除对那份本机配置的路径依赖。
            env_overrides={"PPSSPP_DFX_PROJECT_ROOT": str(work_dir)},
        )

    # ── 单 run ──────────────────────────────────────────────────────────

    def collect(self, scenario: Scenario, run_idx: int) -> dict[str, Any]:
        """采一个 (场景, 轮次) 并落盘；返回 record。"""
        real_mode = self.real_mode or scenario.is_real
        stamp = self._ts.strftime("%Y%m%d-%H%M%S")
        rid = run_id_for("oc", stamp, self.model, self.variant, scenario.id, run_idx)
        sessions_dir = Path(tempfile.mkdtemp(prefix="ppsspp-dfx-oc-sessions-"))

        env = build_server_env(
            real_mode=real_mode,
            fixtures_dir=self.fixtures,
            sessions_dir=sessions_dir,
            src_root=_SRC_ROOT,
            tests_root=_TESTS_ROOT,
        )
        env = isolated_xdg_env(env, run_id=f"{scenario.id}-n{run_idx}")

        # ① 会话预置：opencode 拉起的 server 读到本 run 的 sessions.json
        seed_error: str | None = None
        seeded: list[str] = []
        if self.seed_enabled and scenario.pre_state_sessions > 0:
            result = seed_sessions(
                scenario.pre_state_sessions,
                resolve_iso(real_mode),
                env,
                real_mode=real_mode,
                settle_s=self.real_settle_s if real_mode else 0.0,
            )
            seeded, seed_error = result.session_ids, result.error
            if seed_error:
                progress.emit(f"  [seed] {scenario.id} 预置未成功: {seed_error}")

        # ② 驱动 opencode
        prompt = build_prompt(scenario, real_mode, variant=self.variant)
        dump_path = None
        if self.dump_events:
            dump_path = self.out_path.parent / f"events-{scenario.id}-n{run_idx}.json"

        def _on_line(line: str) -> None:
            progress.emit(f"  | {line[:200]}", min_level=2)

        run_result = None
        error_kind: str | None = None
        driver_error: str | None = None
        # 驱动是否真的跑失败。**与 `error_kind` 分开**：`NO_TOOL_CALLS` 是
        # 内容层诊断（opencode 正常跑完、只是没调到目标工具），把它算作驱动失败
        # 会让 stop_reason 误报成 `driver_failure`，掩盖「模型答了但没调工具」
        # 这一真实事实。
        driver_failed = False
        try:
            run_result = self.runner.run(
                RunOptions(
                    prompt=prompt,
                    title=f"{self.runner.title_prefix}:{scenario.id}:n{run_idx}",
                    on_line=_on_line,
                    dump_path=dump_path,
                )
            )
            error_kind, driver_error = self.runner.classify(run_result)
            driver_failed = bool(error_kind)
        except (ValueError, OSError) as exc:
            # agent 缺失 / bin 不可执行：属于该 run 的失败，不中断整场
            error_kind = (
                ErrorKind.AGENT_MISSING if isinstance(exc, ValueError) else ErrorKind.SPAWN_FAILED
            )
            driver_error = str(exc)
            driver_failed = True
            _log.warning("[%s] 驱动失败: %s", scenario.id, driver_error)

        # ③ 解析 + 评分
        from evals.gates import evaluate, extract_error_code

        parsed = parse_stream(
            run_result.stdout if run_result else "",
            extract_error_code=extract_error_code,
        )
        if parsed.errors:
            # 结构化 error 事件**优先于**按 returncode 的猜测：v2 的 provider
            # 认证/额度错误只出现在事件流里（`provider.auth` + 403），进程同时
            # 非零退出。不优先取它，额度用完会被显示成「server 失联」，处置方向
            # 完全错（换模型/等额度 vs 重启 server）。
            #
            # 注：工具级失败走 ToolCall.is_error，不进这里——本分支只判会话级错误。
            error_kind = classify_stream_error(parsed.errors[0])
            driver_error = f"{parsed.errors[0].type}: {parsed.errors[0].message}"
            driver_failed = True
        elif not error_kind and not parsed.tool_calls and parsed.has_output:
            # 有事件但零目标工具调用——通常是工具名/结构漂移，如实标注。
            # 注意判据是**真值**而非 `is None`：`classify` 成功时返回空串 `""`，
            # 用 `is None` 会让本诊断分支永不触发（正是旧实现零调用无从解释的
            # 同一类问题）。
            error_kind = ErrorKind.NO_TOOL_CALLS
        if error_kind == ErrorKind.NO_TOOL_CALLS:
            _log.warning(
                "[%s] 事件流无可识别 ppsspp 工具调用；非目标工具: %s",
                scenario.id,
                sorted({c.raw_name or c.name for c in parsed.other_tool_calls})[:10] or "(无)",
            )

        tool_calls = [c.to_dict() for c in parsed.tool_calls]
        final_answer = parsed.text
        gates_result = evaluate(
            scenario.raw, {"tool_calls": tool_calls, "final_answer": final_answer}, self.fixtures
        )

        usage = dict(parsed.usage)
        usage.setdefault("input_tokens", 0)
        usage.setdefault("output_tokens", 0)
        usage["cost_usd"] = None
        if run_result is not None:
            usage["attempts"] = run_result.attempts

        stop = _stop_reason(
            parsed_text=final_answer,
            tool_count=len(tool_calls),
            run_ok=not driver_failed,
        )
        wall_ms = int((run_result.duration_s if run_result else 0.0) * 1000)

        record = build_record(
            run_id=rid,
            scenario_id=scenario.id,
            model=self.model,
            variant=self.variant,
            run_idx=run_idx,
            provenance=dict(self.provenance),
            wall_ms=wall_ms,
            tool_calls=tool_calls,
            final_answer=final_answer,
            usage=usage,
            stop_reason=stop,
            gates_result=gates_result,
            # 成功时归一为 None——空串会让 JSONL 消费者多一种「有值但为空」的分支
            error_kind=error_kind or None,
            pre_state={
                "requested": scenario.pre_state_sessions,
                "seeded": len(seeded),
                "error": seed_error,
            },
            other_tool_names=sorted({c.raw_name or c.name for c in parsed.other_tool_calls}),
            stream_errors=[e.to_dict() for e in parsed.errors],
        )
        if driver_error:
            record["error"] = driver_error
        append_record(self.out_path, record)
        return record

    def should_skip(self, scenario_id: str, run_idx: int) -> bool:
        return (scenario_id, self.model, self.variant, run_idx) in self.done

    def mark_done(self, scenario_id: str, run_idx: int) -> None:
        self.done.add((scenario_id, self.model, self.variant, run_idx))


#: 连续多少次 provider 级失败后熔断网格。
#:
#: provider 级失败（额度耗尽 / 认证失效 / 模型不可用）**不会自愈**：继续跑只会
#: 逐条产出 `calls=0` 的同质噪音记录，把真结果淹没在 JSONL 里，还白烧几小时
#: 机时。实测 49 卡全量跑到第 8 张就撞上额度耗尽，剩下 39 张全部是
#: `provider_quota` + `calls=0`——那份文件里真正有效的数据只有 7 条。
#:
#: 阈值取 3：单次 provider 失败可能是瞬时抖动，连续 3 次基本可判定为环境性故障。
CIRCUIT_KINDS = frozenset(
    {
        ErrorKind.PROVIDER_QUOTA,
        ErrorKind.PROVIDER_AUTH,
        ErrorKind.RATE_LIMITED,
        ErrorKind.MODEL_UNAVAILABLE,
    }
)
DEFAULT_CIRCUIT_THRESHOLD = 3


def run_grid(
    collector: Collector,
    scenario_ids: list[str],
    runs: int,
    circuit_threshold: int = DEFAULT_CIRCUIT_THRESHOLD,
) -> tuple[int, int]:
    """串行跑网格（真机独占语义：real 桶不并发）。返回 (passed, total)。

    连续 `circuit_threshold` 次 provider 级失败即熔断剩余网格（见 CIRCUIT_KINDS）。
    """
    total = passed = 0
    consecutive_provider_failures = 0
    tripped_at: str | None = None
    for sid in scenario_ids:
        if tripped_at:
            break
        scenario = collector.scenarios[sid]
        for run_idx in range(1, runs + 1):
            if collector.should_skip(sid, run_idx):
                progress.emit(f"[skip] {sid} n={run_idx} 已记录")
                continue
            total += 1
            progress.emit(f"[run ] {sid} n={run_idx} ...")
            try:
                rec = collector.collect(scenario, run_idx)
            except Exception as exc:  # noqa: BLE001 — 一个坏 run 不杀整场
                _log.warning("[%s] n=%d 采集异常: %s: %s", sid, run_idx, type(exc).__name__, exc)
                progress.emit(f"[FAIL] {sid} n={run_idx}: {type(exc).__name__}: {exc}")
                continue
            collector.mark_done(sid, run_idx)
            passed += int(rec["success"])
            failed = [g for g, ok in rec["gates"].items() if not ok]
            progress.emit(
                f"[{'PASS' if rec['success'] else 'FAIL'}] {sid} n={run_idx} "
                f"calls={len(rec['tool_calls'])} stop={rec['stop_reason']} "
                f"err={rec.get('error_kind')} failed_gates={failed}"
            )
            if rec.get("error_kind") in CIRCUIT_KINDS:
                consecutive_provider_failures += 1
                if consecutive_provider_failures >= circuit_threshold:
                    tripped_at = sid
                    break
            else:
                consecutive_provider_failures = 0
    if tripped_at:
        _log.warning(
            "provider 级失败连续 %d 次，在 %s 处熔断剩余网格——继续跑只会产出同质噪音。",
            circuit_threshold,
            tripped_at,
        )
        progress.emit(
            f"\n[熔断] 连续 {circuit_threshold} 次 {sorted(CIRCUIT_KINDS)[0]} 类失败，"
            f"在 {tripped_at} 处停止。剩余场景未采集。\n"
            f"  处置：充值 / 换 provider（见 evals/README.md「自定义 provider」），"
            f"重跑时本文件已采部分会被 resume 自动跳过。"
        )
    return passed, total


# ── CLI ────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evals.oc",
        description="ppsspp-dfx 盲测采集（opencode 通道）",
    )
    p.add_argument("--scenarios", default="", help="逗号分隔场景 id（默认全部）")
    p.add_argument("--runs", type=int, default=1, help="每格运行次数")
    p.add_argument(
        "--circuit-threshold",
        type=int,
        default=DEFAULT_CIRCUIT_THRESHOLD,
        help="连续多少次 provider 级失败后熔断网格（0=不熔断）",
    )
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--agent", default=DEFAULT_AGENT, help="opencode agent 定义名（空=用缺省）")
    p.add_argument(
        "--variant",
        default="B1",
        choices=["B0", "B1", "B2"],
        help="仅作轨迹标签；本通道只有 B1 有意义（见模块 docstring）",
    )
    p.add_argument("--out", default="", help="输出 JSONL（默认 runs/runs-oc-<date>.jsonl）")
    p.add_argument(
        "--work-dir",
        default="",
        help="opencode 工作目录（须含 .opencode/；默认从包根向上探测）",
    )
    p.add_argument("--real", action="store_true", help="强制 real PPSSPP 模式")
    p.add_argument("--port", type=int, default=0, help="opencode serve 端口（0=自动探测）")
    p.add_argument("--attach", default="", help="外部 opencode serve URL（仅本机，见 attach.py）")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="单 run 硬顶秒数")
    p.add_argument("--dump-events", action="store_true", help="落盘事件流原文")
    p.add_argument("--no-seed", action="store_true", help="跳过会话预置")
    p.add_argument("--no-resume", action="store_true", help="忽略已有 JSONL 重新采")
    p.add_argument(
        "--mcp-key",
        default="ppsspp-dfx",
        help="opencode 里登记的 MCP server 名（预检用）",
    )
    p.add_argument(
        "--skip-mcp-check",
        action="store_true",
        help="跳过 MCP 注册预检（不建议：未接线会导致整场 calls=0）",
    )
    p.add_argument("-v", "--verbose", action="count", default=0, help="进度详细度（0/1/2）")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    progress.set_verbosity(args.verbose)
    if args.verbose >= 1:
        progress.install_stdout_sink()

    if args.variant != "B1":
        _log.warning(
            "variant=%s 在 opencode 通道不生效：agent prompt 由 "
            ".opencode/agents/%s.md 静态定义，采集器无法注入/剥离。"
            "结果与 LLM API 通道的同名变体不可比。",
            args.variant,
            args.agent,
        )

    scenarios, scen_cfg = load_scenarios(_EVALS_DIR)
    fixtures = fixtures_dir(_EVALS_DIR, scen_cfg)
    try:
        wanted = resolve_ids(scenarios, args.scenarios)
    except CollectorError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    out_path = (
        Path(args.out)
        if args.out
        else _EVALS_DIR / "runs" / f"runs-oc-{datetime.now():%Y%m%d}.jsonl"
    )
    work_dir = resolve_work_dir(args.work_dir)

    # 预检：MCP 未接线就别开跑——否则整场 runs 都是 calls=0 的误导性数据
    if not args.skip_mcp_check:
        ready, detail = check_mcp_registered(work_dir, args.mcp_key)
        if not ready:
            print(detail, file=sys.stderr)
            return 2
        progress.emit(f"[mcp] {detail}")

    # 常驻 server：显式 attach 优先（须过 URL 白名单），否则 ensure
    mgr = ServeManager()
    owned = False
    handle = None
    if args.attach:
        server_url = validate_attach_url(args.attach)
        password = None
    else:
        handle = mgr.ensure(port=args.port or None)
        server_url, password, owned = handle.url, handle.password, handle.owned
        progress.emit(f"[serve] opencode server on {server_url}")
    if not args.model:
        print("未指定模型：opencode 在非交互模式会永久阻塞（-m 是必填项）", file=sys.stderr)
        return 2

    collector = Collector(
        scenarios=scenarios,
        fixtures=fixtures,
        out_path=out_path,
        model=args.model,
        agent=args.agent,
        variant=args.variant,
        work_dir=work_dir,
        server_url=server_url,
        server_password=password,
        timeout_s=args.timeout,
        real_mode=args.real,
        seed=not args.no_seed,
        dump_events=args.dump_events,
        resume=not args.no_resume,
    )

    try:
        passed, total = run_grid(
            collector,
            wanted,
            args.runs,
            circuit_threshold=args.circuit_threshold if args.circuit_threshold > 0 else 10**9,
        )
    finally:
        if handle is not None and owned:
            mgr.shutdown(handle)
            progress.emit("[serve] opencode server closed")
    progress.emit(f"\ndone: {passed}/{total} runs passed; JSONL → {out_path}")
    return 0 if total == 0 or passed == total else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
