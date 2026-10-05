"""opencode 采集线 —— **兼容入口，已废弃**（2026-09-30）。

本文件原为单文件实现（399 行，混合 serve 生命周期 / env 拼装 / 子进程驱动 /
事件流解析 / 轨迹构造 / 评分 / CLI）。现实现已按 `porpoless/parse/` 的模块化
理念拆分至 `evals/oc/` 包，本文件仅保留转发以保证既有调用方不破。

## 新入口

```bash
<venv python> -m evals.oc --scenarios CTL-01 --runs 1
<venv python> -m evals.oc --scenarios CTL-01 --runs 1 --dump-events -v
<venv python> -m evals.oc --scenarios R9-REAL-STEP --runs 1 --real
```

## 为什么旧实现采不到东西（保留此说明以便追溯）

`runs-oc-20260929.jsonl` / `runs-oc-20260930.jsonl` 的 CTL-01 均为
`calls=0`、`--dump-events` 落盘 **0 字节**、`stop=max_turns`。真因在解析层：

opencode 把 MCP 工具暴露为 `<serverKey>_<toolName>`，`.opencode/mcp.json` 的
server key 是 `ppsspp-dfx`，事件流里工具名形如 `ppsspp-dfx_ppsspp_health`。
旧 `parse_tool_calls` 用 `name.startswith("ppsspp_")` 过滤，该前缀永不成立，
于是恒返回空列表——**不是模型答错，是解析层把全部调用过滤掉了**。

新实现由 `evals.oc.events.normalize_tool_name` 归一后再比对，并保留
`other_tool_calls` 诊断通道，使同类漂移一眼可见。

其余一并修复的缺陷见各模块 docstring（XDG 隔离清空凭据 / 硬编码 pnpm 路径 /
`wait(timeout=)` 形同虚设 / 停不掉的 server / 无 URL 白名单 / 停不掉的
`stop_reason` 误报）。
"""

from __future__ import annotations

import warnings

from evals.oc.collect import main

__all__ = ["main"]

warnings.warn(
    "evals.opencode_collect 已废弃，请改用 `python -m evals.oc`（实现已迁至 evals/oc/ 包）",
    DeprecationWarning,
    stacklevel=2,
)


if __name__ == "__main__":  # pragma: no cover
    from evals.oc.collect import main as _main

    raise SystemExit(_main())
