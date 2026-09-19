# 协议面范围与事件映射（SCOPE）

> 本文档回答两个问题：PPSSPP WebSocket debugger 的哪些事件被工具化了？
> 哪些刻意没有？事件契约的机器可读单一真相源是
> `src/ppsspp_dfx_mcp/core/ws_contract.py`（逐事件的票据/形状/超时语义），
> 本文是其人读版快照——两者不一致时以代码为准。

## 事件 → 工具映射

| 事件组 | 事件 | 对应工具（ppsspp_ 前缀省略） |
|---|---|---|
| 连接 | `version` / `cpu.status` | `session`（get/list/start/stop/wait_ready）、`health`、`gpu_stats` |
| game | `game.status` `game.start` `game.pause` `game.resume` `game.reset` `game.quit` | `session`（start/stop/get/wait_ready/list）、`step`(pause/resume/reset)、`batch_step` |
| cpu 走查 | `cpu.stepping` `cpu.stepInto` `cpu.stepOver` `cpu.stepOut` `cpu.resume` `cpu.nextHLE` `cpu.runUntil` | `step`（pause/resume/reset/run_until/next_hle）、`frame_snapshot`、`batch_step`(cpu_step)、`context` |
| cpu 寄存器 | `cpu.getAllRegs` `cpu.getReg` `cpu.setReg` `cpu.evaluate` | `query`(registers/register)、`write_register`、`evaluate`、`frame_snapshot` |
| cpu 断点 | `cpu.breakpoint.add/list/remove/update` | `breakpoint`（set/remove/list/update/wait/trace）、`context` |
| 内存读写 | `memory.read` `memory.read_u8/_u16/_u32` `memory.readString` `memory.write` | `read_memory`、`write_memory`、`scan`、`diff_memory` |
| 内存观察 | `memory.breakpoint.add/list/remove/update` | `breakpoint`（mem_set/mem_list/mem_remove/mem_update/mem_update）、`context` |
| 反汇编/汇编 | `memory.disasm` `memory.assemble` `memory.searchDisasm` `memory.base` `memory.mapping` | `disassemble`、`assemble`、`search_disasm`、`memory_map` |
| 搜索 | `memory.info.search` | `search_memory_info` |
| 输入 | `input.buttons.send` `input.buttons.press` `input.analog.send` | `press_button`、`hold_buttons`、`send_analog`、`batch_step`(press)、`wait_frames` |
| GPU | `gpu.buffer.renderColor/renderDepth/renderStencil` `gpu.buffer.texture` `gpu.buffer.clut` `gpu.record.dump` `gpu.stats.get/feed` | `screenshot`、`dump(kind=texture|clut)`、`gpu_record`、`gpu_stats` |
| HLE | `hle.thread.list/stop/wake` `hle.module.list` `hle.func.list/scan/add/remove` `hle.backtrace` | `query`(threads/modules/funcs/func_scan/func_add/func_remove/backtrace)、`analyze_log` |
| replay | `replay.begin/flush/abort/execute/status/time.get/time.set` | `replay`（全部 action，含 `wait_complete`） |
| 广播 | `cpu.stepping`（无票据广播） | 步进广播路由：`breakpoint(action='wait')` 订阅、批处理调度消费（见 `core/batch_jobs.py`） |

上层复合工具（不直接对应单个 WS 事件）：

- `frame_snapshot` = 暂停 + PC/寄存器/探针采样 + 恢复的单调用编排
- `context` = 崩溃归因包（地址身份识别 + 反汇编窗口 + 可选回溯）
- `breakpoint(action='trace')` = 内存断点武装 → 命中捕获（寄存器/回溯）→ 清理 → 恢复
- `health` = 四点会话电池（iso/cpu/ws/game_mode）+ 服务器生存探针
- `scan` = pattern / value / strings 三模内存扫描（可后台化）
- `batch_step` = press/wait/state_probe/cpu_step/screenshot 的后台任务编排
  （detached task，独立于 MCP 工具调用超时；`batch_status`/`batch_cancel` 轮询/取消）
- `state_observer` / `script` / `list_addresses` / `reload_scripts` /
  `analyze_log` / 15 个 `script_*` 动态工具 = 观测注册表与诊断脚本体系
  （种子来自 `.ppsspp-dfx/config/`）

## 已建档但刻意未工具化的事件

| 事件 | 状态 | 理由 |
|---|---|---|
| `broadcast.config.get` / `broadcast.config.set` | 契约已建档，无工具 | 任意 PPSSPP 配置读写尚无稳定的工具级契约（校验/副作用边界未定）；需求出现时先补契约再暴露 |

## 与同类项目的覆盖对比

相对 mcp-bizhawk / mcp-mgba / mcp-ppsspp（dmang-dev）等桥接方案，本服务
额外工具化的 PPSSPP 原生能力：GPU 帧渲染缓冲与纹素/CLUT 转储、GPU 命令流
录制（`gpu.record.dump`）、GPU 性能计数、HLE 线程/模块/符号/回溯全套、
反汇编-汇编-反查三件套、内存观察点（读写/访问 watchpoint）、原生
replay 录制回放、以及基于这些原语的复合编排（快照/追踪/后台批处理）。

## 诚实声明

能力声明只存在于有可用实现之后；`inputSchema`/`outputSchema` 全量结构化。
已知边界（无存档 API、帧推进指令级、VRAM 直读色偏等）汇总在
[README 已知限制](../README.md#已知限制)。
