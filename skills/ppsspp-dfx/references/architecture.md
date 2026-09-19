---
title: PPSSPP 调试架构与配置体系
type: references
category: architecture
---

# PPSSPP 调试架构与配置体系

> 描述 Agent 调用 PPSSPP 调试工具时需要理解的分层抽象、会话模型与配置体系。工具清单见 [tool-surface.md](tool-surface.md)；约束条文见 [ppsspp-constraints.md](ppsspp-constraints.md)；项目上下文由项目专属 skill 提供（不在本仓）。

## 调试 4 层抽象

```
L3: MCP Server    — 36 个静态工具 + 动态脚本工具（Agent 的唯一操作面）
L2: WebSocket API — PPSSPP 原生 WS 调试器（ws://127.0.0.1:<port>/debugger，
                    子协议 debugger.ppsspp.org，连接后必须先发 version 握手）
L1: File Log      — PPSSPP 文件日志（memstick/PSP/SYSTEM/DUMP/log.txt）
                    + 广播日志镜像（.ppsspp-dfx/output/ppsspp.log）
L0: Process       — PPSSPP 进程管理（会话启动/停止、IR Interpreter 模式）
```

## 会话与并发模型

- 一个会话 = 一个 PPSSPP 子进程 + 一条 WS 调试连接；`session_id` 贯穿所有调试工具
- **同会话工具调用被串行化**（等锁 >5s → `SESSION_BUSY`）：`wait_frames`、`batch_step`、录制期间勿并发调用
- 会话持久化于 `~/.ppsspp-dfx/sessions.json`；空闲 30min 自动回收；server 退出时逐会话清理
- 会话启动：随机空闲端口 + 临时 appendconfig ini；Windows 桌面版忽略 appendconfig 时按 PID 经 netstat 发现真实端口。PPSSPP 必须带窗口启动（无窗口会导致 CPU 不启动）
- 断线自动重连一次并重做 version 握手

## 日志镜像链

```
PPSSPP 运行时 log 广播 → MCP GameStateObserver → ppsspp_dfx_mcp.ppsspp_log
  → 镜像 Handler → .ppsspp-dfx/output/ppsspp.log（10MiB 上限）
  → ppsspp_analyze_log 默认数据源
```

离线分析历史崩溃用 PPSSPP 自身文件日志 `memstick/PSP/SYSTEM/DUMP/log.txt`；GPU 崩溃转储为同目录 `*.ppdmp`。

## 配置发现

三层优先级（**从项目根启动**，不向上查找）：

1. 环境变量（`PPSSPP_DFX_WS_HOST`/`PPSSPP_DFX_LOG_LEVEL`/`PPSSPP_DFX_RATE_LIMIT` 等）
2. `<cwd>/.ppsspp-dfx/config/*.yaml`
3. 内置默认值

```
.ppsspp-dfx/
├── config/
│   ├── project.yaml           # PPSSPP exe 路径等项目元数据
│   ├── addresses.yaml         # 地址常量唯一真相源（top_base/game_mode/state_probes/known_functions...）
│   └── scripts.manifest.yaml  # 诊断脚本清单
└── output/                    # 服务器自动写（gitignore）
    ├── screenshots/  textures/  cluts/  gpu_dumps/  replays/
    └── ppsspp.log             # 广播日志镜像
```

编辑 `addresses.yaml` 即时生效，无需改代码。skill 与 playbook 一律引用**符号名**，运行时经 `ppsspp_list_addresses` 取值。

## 脚本系统（动态扩展层）

- `scripts.manifest.yaml` 登记诊断脚本（分类：eboot/state/p0ab/ndx/memory/misc/recipe）；`exposed=true` 且 `status=migrated` 的脚本在 server 启动时注册为 `ppsspp_script_<name>` 动态工具（skeleton 被 preflight 拒绝）
- `ppsspp_run_script(name, input)` 按 manifest 执行；Input 经 Pydantic 校验；`requires_ppsspp=true` 的脚本无会话时报 `SESSION_NOT_FOUND`（工具层强制）
- `ppsspp_reload_scripts` 重读清单、清模块缓存并**同步动态工具注册**（新增/注销使注册表与 manifest 一致，无需重启），返回 added/removed/registered 对账
- **可用性判别**：`status` 字段机器可读（`migrated`=可运行 / `skeleton`=调用返回 `not_implemented`）；当前 exposed 且可用的有 `hello_diagnostic`（离线自检）、`find_0e_source`（追踪 $ra=0x0E0E0E0E 来源）、`check_cpu_state`（CPU 状态一键体检：running/paused/freeze_suspected）
- 脚本需要的地址常量应取自 `ctx.addresses`（addresses.yaml），而非硬编码

## PSP 内存映射

| 区域 | 地址范围 | 用途 |
|------|----------|------|
| kuseg | 0x00000000-0x7FFFFFFF | 用户空间（MMU 映射） |
| kseg0 | 0x80000000-0x9FFFFFFF | 内核非缓存段 |
| 用户 RAM | 0x08800000-0x0A000000 | PSP 主内存（游戏代码+数据） |

受保护写区间（kernel <0x08800000、top.prx 代码段 0x08804000-0x08D34000）见 [ppsspp-constraints.md](ppsspp-constraints.md) C9。

## 关联资源

- 协议约束: [ppsspp-constraints.md](ppsspp-constraints.md)
- CPU 状态契约: [cpu-state-contract.md](cpu-state-contract.md)
- 工具面速查: [tool-surface.md](tool-surface.md)
- 项目特定上下文：由项目专属 skill 提供（不在本仓）
