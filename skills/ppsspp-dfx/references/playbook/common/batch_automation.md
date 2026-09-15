---
title: 批量自动化与状态观察
type: playbook
scope: common
category: batch-automation
---

# Playbook: 批量自动化与状态观察

> 封装 `ppsspp_batch_step`（步骤编排）与 `ppsspp_state_observer`（命名探针）协作范式。spike 验证：U2=录制中 screenshot 互斥；U4=RUNNING 态 `read_uN` 可信。

## 何时使用

- 推进对话/菜单需要组合多次按键 + 等待 + 状态观察
- 验证状态转换（按键前后内存值对比）
- 录制期间的轻量观察（screenshot 不可用）
- 自动化回归序列（按键 + 等待 + 校验内存值）

## 判定信号

`ppsspp_batch_step` 返回聚合字段 + per-step `results[]`：

| 信号 | 含义 | 路由 |
|------|------|------|
| `succeeded=total` 且 `failed=0` | 全部成功 | 继续 |
| `failed>0` 且 `aborted=false` | 部分失败但继续跑（on_failure=continue） | 查 `results[]` 中 `status='error'` 的步骤 |
| `aborted=true` | 首个失败触发中止 | 查 `abort_reason` |
| **整体 isError（`BATCH_STEP_FAILED`）** | 只要有失败/中止步，整个调用按错误收尾 | 这是预期行为：先读结构化结果再决定，不要盲目重试 |
| `skipped>0` | screenshot 步骤因录制中被跳过 | 非失败；回放后再截 |

`ppsspp_state_observer` 返回 `observations[]`：`error` 非空 = 该探针读取失败（查地址/会话）；`success_count=count` = 全部可信。

## 调用顺序

### 1. 探针准备（一次性）

优先用 addresses.yaml `state_probes` 预置探针（首次访问自动播种）。临时探针：

1. `ppsspp_state_observer(action="register", name="<名>", address="<0x...>", size=<1|2|4>, description="<备注>")`
2. `ppsspp_state_observer(action="list")` 确认

> 注册表是**进程级**共享（跨会话可见）；`register` 不写回 YAML；`clear` 后同进程不再自动播种。不同游戏/版本混用时先 `clear`。

### 2. 编排执行

构造 `steps` 数组（每步含 `type`）：

- `{"type":"press","button":"cross","duration":30}` — 按键（单位帧）
- `{"type":"wait","frames":60}` — 等待
- `{"type":"state_probe","names":"game_mode,cursor_x","samples":3}` — 状态观察（可多采样）
- `{"type":"screenshot","source":"render"}` — 截图（录制中自动 skipped）

执行：`ppsspp_batch_step(session_id, steps, on_failure="continue"|"abort")` → 按 §判定信号校验。

### 3. 录制期间盲录（组合 replay）

1. `ppsspp_replay(action="begin")`
2. `ppsspp_batch_step`（steps 只含 press/wait/state_probe）
3. 重复 2 至序列完成
4. `ppsspp_replay(action="save", file_path="rec.ppr")`
5. `ppsspp_replay(action="abort")`

### 4. 回放后批量截图

`ppsspp_replay(action="load")` → `wait_complete` → `ppsspp_batch_step(steps=[screenshot])`。

## 错误处理

| 症状 | 原因 | 修复 |
|------|------|------|
| `step[N] invalid button=...` | 按钮名不在 25 项白名单 | 用 `cross/circle/triangle/square/up/down/left/right/start/select/ltrigger/rtrigger` 等 |
| `unknown probe name(s)` | 探针未注册 | `state_observer(action="list")` 查可用名，或先 `register` |
| `no probes to observe` | 未指定 names 且注册表为空 | 同上 |
| `observations[i].error` 非空 | 地址无效/会话异常 | 校验地址（`ppsspp_list_addresses`）；确认会话存活 |
| 步骤参数超限 | frames/duration >18000 等 | 拆分为多次调用 |
| 整体 `BATCH_STEP_FAILED` 但部分成功 | 预期语义（见判定信号） | 读 `results[]` 定位失败步，只重跑失败段 |

## 关联资源

- 工具语义: [../../tool-surface.md](../../tool-surface.md)（编排与观察族）
- 录制: [replay_recording.md](replay_recording.md)
- 截图降级: [screenshot.md](screenshot.md)
- Smoke 前置: [smoke_test.md](smoke_test.md)
