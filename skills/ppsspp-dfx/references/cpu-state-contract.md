---
title: CPU 状态契约与断点命中协议
type: references
category: protocol
---

# CPU 状态契约与断点命中协议

> 权威表在 MCP `core/ws_contract.py`。本文件回答两个高频问题：这个操作要求 CPU 处于什么状态？断点命中后怎么确认、怎么检查现场？

## 1. CPU 状态四分类

| 类别 | 含义 | 覆盖操作 |
|------|------|---------|
| `NO_STEPPING` | 随时可用 | 断点 CRUD、反汇编、searchDisasm、input.*、memory.mapping、replay.* |
| `AUTO_STEPPING` | PPSSPP 内部短暂锁定，调用方无需做事 | 全部 memory.read/write（读内存在 RUNNING 态即可靠） |
| `REQUIRED_STEPPING` | 必须先暂停，否则 `CPU_STATE_ERROR` | `query(backtrace/func_*)`、`hle.thread.wake/stop`。注意 `write_register`/`evaluate`/`get_pc` 内部已自动暂停恢复，调用方无需手动 pause |
| `REQUIRED_RUNNING` | 必须运行中，暂停则永不返回→超时 | `gpu_stats`、`gpu_record`（MCP 已前置双探针转成干净错误） |

## 2. TrustLevel

- `HIGH` — stepping 后查询，源码保证可信（`get_pc`、safe 查询）
- `MEDIUM` — 内存变量查询，不受 CPU 状态影响
- `LOW` — RUNNING 态裸查询；源码明文 "inaccurate unless stepping"（`cpu.status.pc`、`hle.thread.list.isCurrent`）

## 3. 断点命中协议（CPUCore=2）

### 3.1 前置

- PPSSPP `CPUCore = 2`（IR Interpreter）。JIT 不触发；纯 Interpreter（0）未按项目口径使用。
- 执行断点与内存断点都可在任意 CPU 状态下设置（验证通过：运行态与暂停态设置均正常命中）。

### 3.2 命中检测（wait 工具优先，gpu_stats 为 fallback）

**`ppsspp_query(action="game_state")` 的 `paused` 字段在断点命中时保持 `false`** —— 它反映的是 UI 暂停菜单态，与 CPU stepping 独立（`game.status.paused ≠ Core_IsStepping()`）。用它等命中会永远等不到。

**默认做法：`ppsspp_breakpoint(action="wait", timeout_s=)`** —— 服务端消费 `cpu.stepping` 广播：

- 命中 → `{hit: true, pc, reason, related_address, ticks}`（精确 PC，无需解析报错文本）
- 超时 → `{hit: false}`（**非错误**，可轮询/加大预算重试）
- 调用时 CPU 已暂停 → `{hit: true, already_paused: true, pc}`（高信任 PC）
- 等待期**不占会话锁**：可并发 `read_memory` / `state_observer`（勿发 step/pause/resume——CPU 必须继续跑向断点）

**一步定位内存访问者：`ppsspp_breakpoint(action="trace", address=, read/write/size=, timeout_s=, want_backtrace=)`** —— 设防→等命中→抓现场→清除断点→恢复运行，把 §3.2–§3.4 全链压缩为一次调用（要求游戏 RUNNING；异常路径仍保证清除断点；仅内存断点，执行断点用 `action="set"`+`"wait"`）。

降级 fallback（wait 工具不可用时）：调用 `ppsspp_gpu_stats`——

- 返回 `CPU_STATE_ERROR`，错误信息含 `[stepping0=True, stepping1=True, ...]` → **CPU 已暂停（命中）**
- 正常返回 `fps=...` → 仍在运行，继续触发/等待

辅助确认：`ppsspp_breakpoint(action="mem_list")` 的 `hits` 计数 >0（内存断点）。

### 3.3 命中现场检查

1. `ppsspp_query(action="register", name="pc", safe=true)` — trust HIGH；已暂停时**保持暂停不自动恢复**（后续检查仍有效）
2. `ppsspp_query(action="registers")` — 此刻全寄存器高信任；关注 `a0`-`a3`（参数）、`ra`（返回地址）
3. `ppsspp_disassemble(address=PC)` — 看崩溃/命中点指令流
4. 内存断点命中时 **PC 停在访问指令之后**（访问指令已执行完毕；例：`lbu` 位于 0x...F4，命中 PC=0x...F8）
5. `ppsspp_breakpoint(action="list"/"mem_list")` — 确认断点仍在册（CPU 断点 set/remove 本身无返回，靠 list 回读）

### 3.4 恢复与清理

1. `ppsspp_breakpoint(action="remove", address=...)` / `mem_remove`（先 list 确认在册；`mem_remove` 自动解析真实 size）
2. `ppsspp_step(action="resume")`
3. `ppsspp_gpu_stats` 返回 fps → 确认恢复运行

### 3.5 等待命中的推荐编排

**默认（一次调用替代整个循环）：**

```
ppsspp_breakpoint(action="trace", address=..., read=true, write=true,
                  size=4, timeout_s=30, want_backtrace=true)
→ {hit: true, hits:[{pc, related_address, ...}], bp_removed: true, resumed: true}
```

或分步：设断点（breakpoint set / mem_set）→ `wait_breakpoint(timeout_s=30)` 等命中 → §3.3 深入检查 → §3.4 清理恢复。

**降级 fallback 循环（wait 工具不可用时）：**

```
设断点（breakpoint set / mem_set）
→ step(action="resume")              # 若此前暂停过
→ 循环：press_button / wait_frames 触发场景
       → gpu_stats 探测（CPU_STATE_ERROR=命中 → 退出循环）
       → 超时上限（如 30s）后转"断点不触发"排查
```

注意：断点命中后 CPU 停住，同会话其他调用会正常执行但 PPSSPP 不再推进帧——不要在等待命中期间做长 `wait_frames` 后误判冻结。`wait_breakpoint` 的等待期本身不占锁、也不暂停 CPU，无此问题。

## 4. 超时判定矩阵（`WS_TIMEOUT` 时怎么归因）

| PID 状态 | game 状态 | 判定 |
|---------|----------|------|
| 已死 | — | `WS_DISCONNECTED`（重建会话） |
| 活 | running | `CPU_FREEZE_SUSPECTED`（死循环/HLE 阻塞/GPU 卡管线） |
| 活 | paused | `CPU_STATE_ERROR`（调用方状态用错） |
| 活 | quit | `WS_DISCONNECTED` |
| 活 | loading/未知 | `WS_TIMEOUT`（保守默认，可重试一次） |

## 5. `CPU_FREEZE_SUSPECTED` 恢复工作流

不要 `session(stop)`（会丢现场）。按序：

1. `ppsspp_screenshot` — 拍当前画面留证
2. `ppsspp_step(action="resume")` — 尝试解冻（真死循环可能无效）
3. `ppsspp_query(action="threads")` — 查线程列表（内部 safe 查询，trust HIGH）
4. `ppsspp_disassemble`（先 `get_pc`）— 看 PC 所在指令流

## 6. 协议怪癖速查

- `stepInto` 首次调用只暂停不步进（PPSSPP 已知行为），再调一次才推进
- `stepOver`/`stepOut`/`run_until` 依赖临时断点，地址永不到达→超时是**预期行为**而非故障
- 暂停原语是 `cpu.status.stepping`，与 `game.status.paused` 独立（见 §3.2）
- PPSSPP **先应答 WebSocket 后启动 CPU**——`session(start)` 后必须 `session(action="wait_ready")`，否则早期读报 `CPU not started`；持续失败收尾 `[BOOT_TIMEOUT]`（楔死嫌疑，先查日志再重启）
- `gpu_stats`/`gpu_record` 在暂停态不会"等待"，MCP 直接报 `CPU_STATE_ERROR`——这使其成为最便宜的 CPU 状态探针（降级 fallback）

## 关联资源

- 工具签名与防护语义: [tool-surface.md](tool-surface.md)
- 协议层事实（IR 编码 / VBlank PC / 截图降级）: [ppsspp-constraints.md](ppsspp-constraints.md)
