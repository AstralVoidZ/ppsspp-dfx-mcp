---
name: ppsspp-dfx
description: Debug PSP games in PPSSPP via ppsspp-dfx-mcp. Use for ISO smoke test, crash/freeze analysis, memory scan/patch, breakpoints, register tracing, input automation, replay recording, screenshots, log analysis, or armips patch verification. Do NOT use for non-PSP emulators, PPSSPP end-user settings, or unrelated tasks.
when_to_use: PPSSPP 调试; PSP 游戏调试; ISO 启动冒烟; 崩溃/卡死分析; 内存扫描; 断点追踪; 文本渲染追踪; 按键自动化; 录制回放; 截图取证; 日志分析; armips 补丁验证
metadata:
  version: 3.0.1
  spec: agentskills.io v1
license: MIT
compatibility: Requires the ppsspp-dfx-mcp MCP server and a local PPSSPP with WebSocket debugger (Windows host). Breakpoints require PPSSPP CPUCore=2 (IR Interpreter). Project-specific game context (addresses, function semantics, game-flow) lives in project-side skills, not here.
---

# PPSSPP 调试技能书（通用）

> 适用范围：跨项目通用调试方法论；具体游戏的项目上下文（地址语义、界面状态机、码点约束等）由项目仓库侧的项目专属 skill 承载。工具签名以 MCP `tools/list` 为权威，本文件与其不符时以实际 schema 为准并向用户报告 drift。

## 1. 会话生命周期（所有调试的前置）

1. `ppsspp_health` — 探测 MCP server 存活（不连 PPSSPP）
2. `ppsspp_session(action="start", iso_path=..., resilient=true, wait_ready=true)` — **一步启动并等就绪**（wait_ready=true 复用独立 wait_ready 的探针/预算语义，`[BOOT_TIMEOUT]`=楔死嫌疑）。可选 `resilient=true`：自愈启动——楔死证据（就绪探针耗尽/握手不受理/进程死亡）触发 关闭→隔离 GPU 后端黑名单文件→同 session_id 重启（≤2 次），响应 `recovered=N`（>0 表示现场已重置，断点需重设）
3. `ppsspp_smoke_test(session_id)` — 启动健康四项检查（iso_loaded / cpu_running / ws_connected / game_mode_valid）
4. 高频五工具（`read_memory` / `disassemble` / `get_pc` / `step` / `screenshot`）的 `session_id` **可省略**（唯一活跃会话自动解析；0 会话报错提示启动，多会话报 `SESSION_AMBIGUOUS` 并列出全部 id）；其余工具仍必填
5. `ppsspp_session(action="stop", session_id=...)` — 结束（勿直接杀进程，会泄漏）

约束：同一会话的工具调用被串行化（等锁超 5s 报 `SESSION_BUSY`）；`wait_frames`、`batch_step`、录制期间不要并发调用同会话；空闲 30 分钟会话被自动回收。

## 2. 工具选择默认值

| 目的 | 默认做法 | 禁止/替代 |
|------|---------|----------|
| 看指令 | `ppsspp_disassemble` / `ppsspp_search_disasm` | 禁止 `read_u32` 读代码段（JIT-IR 下得 IR 编码 0x68xxxxxx） |
| 读变量/文本 | `ppsspp_read_memory(action="read_bytes")` + 自行 decode；大块读取（≥数 KB）加 `output="file"`（落盘回路径+64B 预览，省上下文） | `read_string` 仅用于纯 ASCII（多字节会被截断语义） |
| 扫内存 | `ppsspp_read_memory(action="scan")` | 区间 ≤256MiB；不可读区域被静默跳过 |
| 查 PC/寄存器 | `ppsspp_get_pc`（高信任） | RUNNING 态裸 PC 是 LOW trust（VBlank 误导） |
| 暂停抓现场 | `ppsspp_frame_snapshot`（pause→pc+寄存器→resume 一次完成） | 已暂停的 CPU 保持暂停不恢复；可选 `probes` 并采观察探针 |
| 设断点 | `ppsspp_breakpoint`（设防）+ `ppsspp_wait_breakpoint`（等命中） | 需 CPUCore=2；一步定位访问者用 `ppsspp_trace_memory_access` |
| 观察运行态 | `ppsspp_state_observer` / `ppsspp_batch_step` | `gpu_stats`/`gpu_record` 必须在 CPU running 时调 |

## 3. 场景路由表

| 用户需求 | Playbook |
|---------|----------|
| "推进游戏流程/玩游戏/进入游戏/跳过标题/导航菜单" | [common/gameplay_loop](references/playbook/common/gameplay_loop.md) |
| "ISO 加载失败/启动验证" | [common/smoke_test](references/playbook/common/smoke_test.md) |
| "崩溃/卡死根因分析" | [common/crash_analysis](references/playbook/common/crash_analysis.md) |
| "截图/画面捕获" | [common/screenshot](references/playbook/common/screenshot.md) |
| "录制与回放" | [common/replay_recording](references/playbook/common/replay_recording.md) |
| "批量按键 + 状态观察" | [common/batch_automation](references/playbook/common/batch_automation.md) |

地址常量一律查 `.ppsspp-dfx/config/addresses.yaml`（`ppsspp_list_addresses` 可列出）。IDA 偏移 ↔ 运行时地址的离线换算用 `scripts/addr_convert.py`（自动读取 addresses.yaml 基址，无需活跃会话；运行中会话内也可用 `ppsspp_convert_address`）。具体游戏的界面签名、函数语义、码点约束等**项目上下文不在本技能书**——项目仓库如有对应的项目侧 skill（如 `ppsspp-dfx-<game>`），先确认其可用并优先遵循。

## 4. 通用约束（跨场景）

1. **地址参数**一律写全 `"0x"` 前缀 hex 字符串；返回地址以 `0x%08X` 回显。**裸数字串不会被拒绝——它按十进制解析**：`"08804000"` 变成 8804000 = `0x008656A0`，在 32 位范围内、看起来合理、但不是你要的地址，且调用成功返回。漏 `0x` 是静默错误，务必比对回显地址。
2. **寄存器名**只用 MIPS ABI 名（`a0`/`v0`/`t9`/`sp`/`ra` + `pc`/`hi`/`lo`）；`r5` 会被归一为 `v1`，`$` 前缀自动剥除。
3. **断点命中检测**：默认用 `ppsspp_wait_breakpoint(session_id, timeout_s)`——消费 `cpu.stepping` 广播，命中返回 `{hit:true, pc, reason, related_address}`，超时返回 `{hit:false}`（**非错误**，可轮询）；等待期间**不占会话锁**，可并发 `read_memory`/`state_observer`（但勿发 step/pause/resume）。一步到位用 `ppsspp_trace_memory_access(address, access, timeout_s, want_backtrace=)`——设防→等命中→抓现场→清除断点→恢复运行一次完成（`game_state.paused` 在命中时**不变**，它是 UI 暂停菜单态）。降级 fallback：`ppsspp_gpu_stats` 返回 `CPU_STATE_ERROR`（stepping=True）即已命中（见 cpu-state-contract §3）。
4. **CPU/线程数据可信性**：`backtrace`/`threads`/`func_*` 查询必须先 `step(action="pause")`；`get_pc` 内部自动暂停-恢复。
5. **读多字节文本**用 `read_bytes` 取字节后交 `scripts/decode_text.py` 解码（`--encoding auto` 尝试 utf-8/shift_jis/gbk 并标注歧义；多字节文本禁用 `read_string`，见 §2）。
6. **单次读上限** `read_bytes` 65536 字节，超出拆多次。
7. **按键/等待单位是帧**（60fps），`press duration` 与 `wait frames` 上限 18000；`interval` ∈ [0.0001, 1.0]s。
8. **写内存/汇编**受保护区（kernel <0x08800000、游戏代码段）需 `force=True`；`assemble` 按 `\n`/`;` 逐条汇编。
9. **图像工具返回两个通道**（`ppsspp_screenshot` / `ppsspp_dump_texture` / `ppsspp_dump_clut`）：像素在 `content` 的 ImageContent 块，**元数据只在 `structuredContent`**——没有文本块承载元数据，`image_base64` 也不在结构化通道里。三者都**自动落盘**，元数据的 `file_path` 就是已保存的文件：复用那张图，别重复截。

## 5. 错误码恢复矩阵

| 错误/症状 | 恢复路径 |
|----------|---------|
| `SESSION_NOT_FOUND` | 会话不存在或已回收 → 重新 `session(start)` |
| `SESSION_BUSY` | 同会话有长操作占锁 → 等待其完成，勿并发（`wait_breakpoint`/`trace_memory_access` 的等待期**不占锁**，可并发读） |
| `BOOT_TIMEOUT` | 启动 75s CPU 未就绪（楔死嫌疑）→ `ppsspp_analyze_log` 查启动错误（GPU backend 失败记录是已知根因）→ `session(stop)` 后重启会话 |
| `CPU_FREEZE_SUSPECTED` | **不要 stop/重启会话**。`screenshot` 拍现场 → `step(resume)` 尝试解冻 → `query(threads)` → `disassemble` 看 PC 指令流 |
| `WS_TIMEOUT` | 保守默认（PID 状态未知）→ `session(get)` 查健康后再决定 |
| `WS_DISCONNECTED` | PPSSPP 进程死/连接断 → `session(start)` 重建 |
| `PROTECTED_ADDRESS` | 写入受保护区 → 确认意图后加 `force=True` |
| `IR_ENCODING_DETECTED` | 用 `read_u32` 读了代码段 → 改 `disassemble` |
| `CAPTURE_EMPTY` | texture/clut 空捕获 → 进到有渲染的场景再抓 |
| `BATCH_STEP_FAILED` | batch 有失败步（整体 isError）→ 按 `results[]` 逐个排查 |
| `ADDR_INVALID` | 地址格式/范围错 → 按 `"0x..."` 字符串重试 |
| 断点不触发 | 确认 CPUCore=2（IR Interpreter，JIT 不触发）；确认目标地址确有执行流经过 |
| 截图空帧 | 标题/加载屏 render 无内容 → 工具自动回退 VRAM（颜色不可靠）；进场景后再截 |
| 菜单按键无效 | 用完整序列（Start → 确认键 → 等待 → 确认键），单次按键常无效 |

> 本表为高频症状矩阵。全部错误码的语义与恢复路径全表见 [references/error-codes.md](references/error-codes.md)。

## 6. 按需加载参考

- 需要确认某工具的参数细节、防护语义、上限 → 读 [references/tool-surface.md](references/tool-surface.md)
- 任何错误码的语义/恢复路径不确定，或矩阵里查不到当前错误 → 读 [references/error-codes.md](references/error-codes.md)（全表）
- 遇到 `CPU_STATE_ERROR`/`WS_TIMEOUT`/`CPU_FREEZE_SUSPECTED`，或需判断某操作在哪个 CPU 状态合法、断点命中现场检查 → 读 [references/cpu-state-contract.md](references/cpu-state-contract.md)
- 协议层异常（WS 握手、IR 编码、PC 不可信、截图降级）→ 读 [references/ppsspp-constraints.md](references/ppsspp-constraints.md)
- 架构与配置体系（4 层抽象、配置发现、日志镜像、脚本系统）→ 读 [references/architecture.md](references/architecture.md)

## 7. 逃生舱

需要本技能书未覆盖的深度诊断时：`ppsspp_list_scripts` 查 manifest 脚本（注意 `[skeleton]` 标记的尚不可用）→ `ppsspp_run_script(name=...)` 执行；或现场写一次性 Python 经 WS 协议完成（须遵守 ppsspp-constraints 的 version 握手约束）。不要 import MCP 内部模块充当 API。
