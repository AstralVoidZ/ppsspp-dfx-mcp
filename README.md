# ppsspp-dfx-mcp

一个把 [PPSSPP](https://www.ppsspp.org/) 变成 AI 可调试目标的 MCP（Model Context
Protocol）服务器。它把 PSP 模拟器的 WebSocket 调试器封装为面向 LLM agent 的工具面：
会话生命周期、内存读写、反汇编、断点、CPU 控制、输入自动化、截图、回放录制与诊断
脚本——并内建结构化契约、防御性错误分类法与任务级评估。

## 功能特性

- **41 个静态工具**，全部带结构化 `inputSchema` / `outputSchema`——没有无约束的
  返回值，每个参数都有类型和说明。
- **动态脚本工具**：项目专属的诊断脚本通过 `scripts.manifest.yaml` 暴露为
  `ppsspp_script_<name>` 工具，输入类型由脚本自带的 Pydantic model 决定；
  `ppsspp_run_script` 调用未暴露的脚本，`ppsspp_list_scripts` 查看清单——
  详见[配置](#配置)。
- **会话模型**：支持多个并发 PPSSPP 会话、就绪探测（`wait_ready`）与楔死自愈
  （`resilient` 启动）。
- **面向 agent 的人体工学**：组合工具（`ppsspp_frame_snapshot`、
  `ppsspp_trace_memory_access`、`ppsspp_batch_step`）、`session_id` 自动解析、
  防御性错误码（`[CODE] message` 格式、CPU 冻结与连接断开的区分），错误文本内嵌
  恢复建议。
- **后台自动化**：批量任务跑在独立的服务端任务上，不受 MCP 客户端工具调用超时的
  影响；支持状态轮询、取消与注册表盘点（`ppsspp_batch_list`）。
- **内建评估体系**（`evals/`）：21 张场景卡 + 确定性门禁 + 对录制夹具的盲测
  runner + 汇总报告——工具面按 agent 实际使用的方式被测试。
- **诚实的协议面**：能力只在其背后存在可用实现时才声明；刻意置 `false` 的开关
  附有设计理由说明。

## 环境要求

- Python 3.14+，配合独立 venv（原因见下文）
- 带 WebSocket 调试器的 PPSSPP 构建（服务器负责启动它，并连接
  `ws://<host>:<port>/debugger`）
- 一个 MCP 客户端（ZCode、Claude Desktop、MCP Inspector 等）

## 安装

本服务器导入 MCP SDK v2（`mcp.server.mcpserver`），它无法与许多其他 MCP 服务器
锁定的 1.x `mcp` 包共存。请使用自带的引导脚本创建独立 venv：

```bash
# 在本目录执行——创建 .venv/ppsspp-dfx-mcp 并安装（editable，含 dev 依赖）：
python scripts/check_env.py --bootstrap

# 校验解释器 / SDK 版本 / 包导入：
python scripts/check_env.py --check
```

把服务器注册到你的 MCP 客户端。本目录已附带可直接使用的 `.mcp.json`——让客户端
读取它，或按同样的结构内联：

```json
{
  "mcpServers": {
    "ppsspp-dfx": {
      "command": ".venv/ppsspp-dfx-mcp/Scripts/python.exe",
      "args": ["-m", "ppsspp_dfx_mcp"],
      "cwd": "${CLAUDE_PROJECT_DIR}"
    }
  }
}
```

`cwd` 必须是同时存放 `.venv/` 与 `.ppsspp-dfx/` 配置的目录（独立检出时即仓库根）。
POSIX 上请用 `.venv/ppsspp-dfx-mcp/bin/python` 代替 `Scripts/python.exe`。

> 不要在客户端与服务器之间插入包装脚本：Windows 上 `os.execv` 是
> `CreateProcess` + 父进程等待（不是 POSIX 进程替换），多一层会让最内层服务器
> 立即读到 stdin EOF 并静默退出——表面现象只是 `-32000: Connection closed`。

## 使用

```bash
.venv/ppsspp-dfx-mcp/Scripts/python -m ppsspp_dfx_mcp   # Windows
.venv/ppsspp-dfx-mcp/bin/python -m ppsspp_dfx_mcp        # POSIX
```

然后直接给 agent 派任务：*"启动模拟器加载这个 ISO，告诉我当前 PC"*——服务器
负责会话启动、就绪探测与状态读取。工具描述遵循
PURPOSE / USAGE / BEHAVIOR / RETURNS 约定，错误路径内嵌恢复指引，agent 无需
示例即可自助。

## 配置

环境变量（全部可选）：

| 变量 | 默认值 | 说明 |
|-----|---------|-------------|
| `PPSSPP_DFX_LOG_LEVEL` | `INFO` | 日志级别 |
| `PPSSPP_DFX_LOG_FORMAT` | `text` | 日志格式（`text` 或 `json`） |
| `PPSSPP_DFX_RATE_LIMIT` | `60` | 单工具限流（次/分钟，0 为关闭） |
| `PPSSPP_DFX_WS_HOST` | `127.0.0.1` | PPSSPP WebSocket 主机 |
| `PPSSPP_DFX_WS_PORT` | `12345` | PPSSPP WebSocket 端口 |
| `PPSSPP_DFX_EXE_PATH` | （来自 yaml） | PPSSPP 可执行文件路径 |
| `PPSSPP_DFX_SESSIONS_PATH` | `~/.ppsspp-dfx/sessions.json` | 会话状态路径 |

项目级 YAML 配置位于 `.ppsspp-dfx/config/`（相对工作目录）：

- `project.yaml` — `ppsspp_exe` 路径与项目元数据
- `addresses.yaml` — 命名地址常量（同时为内存向导的 `completions`
  能力提供候选）
- `scripts.manifest.yaml` — 诊断脚本清单。每个条目带机器可读的 `status`
  （`migrated` = 可运行，`skeleton` = 方法体返回 `not_implemented`）。标记
  `exposed: true` 的脚本在启动时注册为 `ppsspp_script_<name>` 工具——skeleton
  除外，preflight 会拒绝它们。`ppsspp_reload_scripts` 将动态工具注册表与清单
  重新同步（无需重启），并报告声明与注册的对账结果。

### 独立部署快速开始

三份配置文件的开箱模板见 [`examples/`](examples/)——从这里开始，不要从零手写
YAML：

```bash
mkdir -p .ppsspp-dfx/config
cp examples/project.yaml examples/addresses.yaml \
   examples/scripts.manifest.yaml .ppsspp-dfx/config/
# 然后编辑 .ppsspp-dfx/config/project.yaml：把 ppsspp_exe 指向你的
# 带 WebSocket 调试器的 PPSSPP 构建；把 addresses.yaml 里的 PLACEHOLDER
# 地址替换为你自己逆向得到的值。
```

首次会话前需要知道的两件事：

- 没有 `scripts.manifest.yaml` 服务器仍能启动，但所有 `ppsspp_script_*` 工具会
  静默消失——即使 `scripts:` 列表为空也请保留模板（`check_env.py --check`
  报的正是这个警告）。
- 配置为空且无占位值时，服务器侧一切功能可用；只有会话启动需要真实的
  `ppsspp_exe`（或 `PPSSPP_DFX_EXE_PATH`），地址常量也只有在你提供自己游戏的
  数值后才有意义。

## 协议面

在 `initialize` 握手时声明——且**只**声明实际注册的能力（SDK 从请求处理器
是否存在来推导各项能力，所以这里出现的每一项背后都有可用实现）：

| 能力 | 声明 | 说明 |
|---|---------|-------|
| `tools` | ✅ | 41 个静态工具 + 动态 `ppsspp_script_<name>` |
| `resources` | ✅ | `ppsspp://game-state`、`ppsspp://registers`（快照） |
| `prompts` | ✅ | `memory-breakpoint-wizard`、`memory-trace-wizard` |
| `completions` | ✅ | 两个内存向导的 `address` 参数，候选来自 `addresses.yaml` |
| `logging` | ❌ | 协议修订 2026-07-28 移除了 `logging/setLevel` |
| `tasks` | ❌ | 仅 SDK 2.2.0 的类型定义，无服务器端实现 |

`tools.list_changed` 与 `resources.subscribe` 刻意置 **`false`**。SDK 2.2.0 的
`MCPServer` 没有暴露握手期设置 `notification_options` 的入口，声明它们等于承诺
一个服务器发不出的通知。现有替代：

- `ppsspp_reload_scripts` 会**报告**变化内容（`exposed_added` /
  `exposed_removed`），agent 无需通知通道即可响应。
- 服务器 `instructions` 字符串告诉新 agent 工具面包含什么。

若未来 SDK 开放了该入口，翻转开关并补上 `send_*_list_changed` 调用即可——L2
契约测试（`tests/unit/l2_mcp_contract/test_capabilities_contract.py`）断言当前
的 `false` 状态并会失败，这是设计信号：该决策需要重新审视，而非回归。

### 返回形态

**图像类工具**（`ppsspp_screenshot`、`ppsspp_dump_texture`、
`ppsspp_dump_clut`）返回拆成两半的 `CallToolResult`：

- `content` — 一个携带像素的 `ImageContent` 块。
- `structuredContent` — 仅元数据（`file_path` / `size_bytes` / `format`，加上
  `mode`、`width`、`height`、`empty` 等各工具自有字段）。图像的 base64 副本
  **不在**这个通道里——那会膨胀 schema，且重复 `content` 已承载的内容。

每个工具都声明结构化 `outputSchema`——没有工具返回无约束对象或 `items` 为空的
数组。`ppsspp_run_script` 的 `input` 参数是唯一注册在案的例外：其形状由被调用的
脚本决定，因此只描述而不约束。

## 错误处理

当被模拟的 CPU 冻结（死循环 / HLE 阻塞 / GPU 管线停滞）时，服务器返回
`CPU_FREEZE_SUSPECTED` 而不是笼统的 `WS_DISCONNECTED`——区分"PPSSPP 进程还
活着但 CPU 冻结"与"进程已死 / WebSocket 断开"。

对 `CPU_FREEZE_SUSPECTED` 的建议处理：

- **不要**重启会话——PPSSPP 还在运行。
- 用 `ppsspp_screenshot` 截取当前画面辅助诊断。
- 尝试 `step(action='resume')`（对真正的死循环可能无效）。
- 用 `hle.thread.list` 查看线程状态（可能暴露 HLE 阻塞）。
- 在当前 PC 处用 `ppsspp_disassemble` 检查指令流。

相关错误码：`WS_DISCONNECTED`（PID 已死，真断开）、`WS_TIMEOUT`（带票据的
RPC 超时，保守默认）、`CPU_STATE_ERROR`（当前 CPU 状态不适合该操作）。错误
文本始终以 `[CODE]` 开头，agent 可编程分类；存在下一步的地方都内嵌了恢复建议。

## 开发

文档/注释规范与测试工作流见 [CONTRIBUTING.md](CONTRIBUTING.md)。要点：

```bash
# 全量测试套件（单元 + 契约 + 集成；约 1500 个测试）：
.venv/ppsspp-dfx-mcp/Scripts/python -m pytest tests -q

# 工具签名/描述变更后重新生成工具面基线（与变更同笔提交）：
.venv/ppsspp-dfx-mcp/Scripts/python scripts/dump_tool_surface.py
```

`evals/` 目录承载盲测评估体系（场景卡、确定性门禁、runner、报告）——见
`evals/README.md`。

## 许可证

[MIT](LICENSE)
