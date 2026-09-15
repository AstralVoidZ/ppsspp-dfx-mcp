---
title: 录制与回放
type: playbook
scope: common
category: replay
---

# Playbook: 录制与回放

> 封装 PPSSPP 内置 Replay 子系统（`replay.*` WS 事件）。关键不变式经 spike 验证：U2=录制期间 screenshot 互斥；U3=`execute` 不会自动结束；U4=RUNNING 态 `read_uN` 可信。

## 何时使用

- 录制操作序列用于自动化测试或回归验证
- 验证行为可重现性（同 ISO + 同录制数据 + 同 base_rtc → 相同结果）
- 固化复杂按键序列供批量回放（对话推进、菜单导航）
- 持久化到 `.ppr` 供跨会话复用

## 判定信号

| 字段 | 含义 | 路由 |
|------|------|------|
| `saving=true` | 录制中 | 可继续按键 → `flush`/`save` |
| `executing=true` | 回放中（**不会自动结束**，U3） | 必须 `wait_complete` 或 `abort` |
| `version>0` + `base64` 非空 | `flush` 成功 | 保存两者供 `execute` |
| `size=0` 且 `base64=""` | 未录到事件 | 确认 `saving=true` 成立；按键须经 MCP 工具而非 PPSSPP GUI |
| `base_rtc` | 基准 RTC（秒） | 跨会话回放需 `time_set` 恢复（`load` 默认自动恢复） |
| `wait_iterations` 异常大 | 回放卡住 | `abort` 后检查录制时序 |

## 调用顺序

### 录制

1. `ppsspp_replay(action="status")` — 确认 IDLE（`executing=false` 且 `saving=false`），否则先 `abort`
2. （可选）`ppsspp_replay(action="time_get")` — 记录 `base_rtc`
3. `ppsspp_replay(action="begin")` — 进入录制
4. `ppsspp_press_button` / `ppsspp_batch_step`（press+wait+state_probe，**不含 screenshot**，U2）驱动场景
5. `ppsspp_replay(action="save", file_path="rec.ppr", session_note="<描述>")` — 一次完成 flush + time_get + 写盘；或 `flush` 手动取数据后 `abort`

### 回放

1. `ppsspp_replay(action="status")` — 确认 IDLE
2. （跨会话）`ppsspp_replay(action="time_set", value=<base_rtc>)`
3. `ppsspp_replay(action="execute", version=<V>, base64_input=<B>)`
4. `ppsspp_replay(action="wait_complete", timeout_ms=10000, interval_ms=100)` — **必须**（U3）
5. `ppsspp_replay(action="status")` — 确认 `executing=false`；此后 screenshot 恢复可用

### 复用（load）

1. 确认 IDLE → 2. `ppsspp_replay(action="load", file_path="rec.ppr")`（读盘 + 默认恢复 base_rtc + execute）→ 3. `wait_complete` → 4. `status` 确认结束

### 录制期间观察（盲录）

screenshot 不可用（U2），改用内存观察（U4 保证可信）：

- `ppsspp_state_observer(action="observe", names="game_mode,...")` 或 `read_memory(read_u32)` 读状态变量
- 快变地址（帧计数器类）多次采样取众数

## 约束

- `file_path` 只允许**裸文件名**（含目录成分直接被拒），恒落 `.ppsspp-dfx/output/replays/`
- `.ppr` 为 JSON：`ppr_format_version` / `version` / `base64` / `base_rtc` / `recorded_at` / `session_note`
- 录制要求 CPU running（输入时序真实）；跨 PPSSPP 进程的完全确定性还需相同初始 RAM（save state），当前未支持

## 错误处理

| 症状 | 原因 | 修复 |
|------|------|------|
| `execute` 后长时间不结束 | 回放卡住（录制时序与现场状态不匹配） | `abort`；缩短录制段重录 |
| `flush` 返回空 | 录制期间无按键事件 | 确认 `saving=true`；按键走 MCP 工具 |
| `execute` 报 version/base64 缺失 | 未传或传 0/空 | 必须来自 `flush` 返回值 |
| screenshot 步骤被 skipped | 录制中（batch 自动跳过，非失败） | 回放后再截 |
| `save` 写盘失败但拿到了 base64 | 落盘错误 | 错误消息内嵌完整 version+base64，可直接用于 `execute` 恢复 |
| `load` 报 JSON/schema 错误 | `.ppr` 损坏或版本不符 | 重新 `save`；确认 `ppr_format_version` |

## 关联资源

- 工具语义: [../../tool-surface.md](../../tool-surface.md)（编排与观察族）
- 批量编排: [batch_automation.md](batch_automation.md)（盲录模式）
- 截图: [screenshot.md](screenshot.md)（回放后观察）
- Smoke 前置: [smoke_test.md](smoke_test.md)
