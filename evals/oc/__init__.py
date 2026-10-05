"""opencode 采集线（ppsspp-dfx 盲测评估的第二条采集通道）。

## 为什么有两条通道

| | LLM API 通道 | opencode 通道（本包） |
|---|---|---|
| 大脑 | 自建 agent loop + 用户 API key | 外部 agent 自带额度 |
| 依赖 | 需要 `llm_api.json`（含密钥） | **免 key**——很多套餐不提供 API |
| MCP 会话 | Python 侧直连 server | opencode 拉起（`.opencode/mcp.json`） |
| 资源复用 | 每 run 一个 server 子进程 | 常驻 server，PPSSPP 长驻 |

两条通道的**评分面完全共用**（`scenarios.yaml` + `gates.py` + 轨迹 schema），
故 runs 可合并对账（设计 D3）。

## 模块划分（对标 porpoless `parse/`）

| 模块 | 职责 | 对标 |
|---|---|---|
| `log` | logger 工厂 + 吞咽留痕 | `porpoless/log.py` |
| `progress` | 进度通道（与诊断分离） | `porpoless/progress.py` |
| `errors` | 结构化失败信号 `error_kind` | `porpoless/parse/runner_base.py` |
| `events` | 事件流解析（工具名归一 / usage 聚合） | `porpoless/parse/opencode_events.py` |
| `attach` | `opencode serve` 生命周期 + URL 白名单 | `porpoless/parse/attach.py` |
| `runner` | `opencode run` 驱动（flag 探测 / 超时安全） | `porpoless/parse/cli_runner.py` |
| `env` | server env + XDG 隔离 + 会话预置 | 本通道新增（消除三处重复） |
| `scenario` | 场景卡访问层 | `porpoless/parse/batching.py` |
| `trajectory` | 轨迹契约 + JSONL + resume | `porpoless/parse/corpus_io.py` |
| `collect` | 编排 + CLI | `porpoless/cli.py` |
"""

from __future__ import annotations

from evals.oc.errors import CollectorError, ErrorKind
from evals.oc.events import ParsedStream, ToolCall, normalize_tool_name, parse_stream
from evals.oc.log import get_logger, log_swallow
from evals.oc.runner import CliRunner, OpencodeRun, RunOptions
from evals.oc.trajectory import append_record, build_record, done_keys

__all__ = [
    "CliRunner",
    "CollectorError",
    "ErrorKind",
    "OpencodeRun",
    "ParsedStream",
    "RunOptions",
    "ToolCall",
    "append_record",
    "build_record",
    "done_keys",
    "get_logger",
    "log_swallow",
    "normalize_tool_name",
    "parse_stream",
]
