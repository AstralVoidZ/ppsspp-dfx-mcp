---
title: 游玩闭环（推进游戏流程）
type: playbook
scope: common
category: gameplay-loop
---

# Playbook: 游玩闭环（推进游戏流程）

> 让 agent 能可靠"玩游戏"——从开机推进到目标场景，并在场景间移动。核心是**闭环节拍**（按键→观测→分支），替代盲按+长等的开环模式。项目界面签名与按键语义查项目侧 game-flow 文档与 `addresses.yaml` 的 `game_flow:` 节。
>
> **适用边界**：本文节拍仅适用于**游戏内**界面（对话/菜单等无回退惩罚场景）；标题/OP/吸引模式层存在自动回退，禁用本文节拍，见下节。

## 回退规避（idle-timeout 界面层通用模式）

许多游戏（尤其合集盘/带吸引动画的）的标题/OP/PRESS BUTTON 层有**逐级自动回退**（典型：菜单静置 15-40s 开始淡出，40-70s 回到 OP 重播）。规则：

1. **这些层禁止用本文节拍逐键推进**——用项目标定的标准序列（单个 batch_step 原子执行，段间不留等待）。
2. 到达标题菜单后，下一步操作必须在**静置安全窗口内**（典型 15s 量级）发出。
3. 界面判定按"日志签名 > 探针 > 截图终审"阶梯，不要连续截图轮询——截图间隙本身就是回退窗口。
4. OP/吸引动画跳过优先试 start；菜单确认键以项目标定为准（start 在部分稳定菜单上不可靠）。
5. 项目的回退状态机签名表与标准序列属项目上下文，记录在项目侧 skill（本仓不含）。

## 何时使用

- 从开机推进到目标场景（菜单/对话/战斗/字库渲染画面）
- 在场景间移动（跳过过场、导航菜单、推进对话）
- 回归测试需要可复现地"到达"某个游戏状态
- 项目专属 playbook 的前置步骤（它们假设"已到达目标场景"）

## 判定信号

| 信号 | 含义 | 路由 |
|------|------|------|
| `state_probe` 值在相邻轮变化 | 按键生效，场景推进中 | 继续当前节拍 |
| 连续 2 轮 probe 值无变化 | 按键未生效或画面不吃输入 | 轮换按键（cross↔start↔方向键）；仍无变化转场景判断 |
| 截图 `empty=true` / `render→vram_fallback` | 加载屏/未渲染画面 | 只等待（每轮 60 帧），不按键 |
| `check_cpu_state` 返回 paused | 断点命中 | 转断点流程（cpu-state-contract §3），游玩暂停 |
| game_mode 值变化 | 场景切换 | 对照 项目界面签名表识别新场景 |
| batch 整段完成但 game_mode 仍为初始值 | 卡死/按键序列不完整 | 换宏或对照项目侧标准序列重试 |

## 标准节拍（闭环）

**一次 `ppsspp_batch_step` 调用 = 多轮"按键→等待→观测"**，禁止拆成多次调用手搓（每步一次调用的轮次成本会拖垮会话，也拉长无谓等待）：

```
steps = [
  {"type":"press","button":"start","duration":30},
  {"type":"wait","frames":60},
  {"type":"state_probe","names":"game_mode","samples":2},
  {"type":"press","button":"cross","duration":30},
  {"type":"wait","frames":60},
  {"type":"state_probe","names":"game_mode","samples":2},
  ... 重复至多 10 轮
]
on_failure = "continue"   # 单轮失败不中断，看整体 results
```

节拍参数经验值：按键 30 帧（≈0.5s）→ 等待 30-90 帧（动画过场取上限）；**单次 batch ≤10 轮、总帧 ≤300**（超限拆多次调用——batch 占会话锁，长批会撞 SESSION_BUSY）。

## 场景分类决策树

每轮观测后先分类再行动（界面签名查项目侧签名表，此处是通用规则）：

1. **标题/PRESS BUTTON/开场过场**——**等待永远无效**（attract mode 会超时回退），必须持续输入：start/cross 轮换，每 30-60 帧一次。
2. **菜单界面**——读当前选中项（光标变量或截图语义），用方向键移动 + cross 确认；选错用 circle 返回。导航前先确认所在菜单页（防串页）。
3. **加载/黑屏**（截图 empty 或 vram_fallback）——只等待，不按键；连续 3 轮（≈180 帧）仍是加载态再评估。
4. **对话/事件文本**——cross 节拍推进；出现选项时截图读选项再选。
5. **游戏主循环**——按任务需求行动（触达目标场景后转交对应 playbook）。

分类依据优先级：**内存签名（game_mode/光标变量）> 截图语义**。两者冲突时以内存为准并截图留证。

## 防呆规则（必须遵守）

1. **被动惩罚**：标题/过场画面不会自己前进。任何"等待动画播完"的策略在 attract mode 下都会回退——推进靠输入，不靠等待。
2. **无变化轮换**：连续 2 轮 probe 无变化 → 换一个按键再试 2 轮；仍无变化 → 截图判断场景是否为加载屏。
3. **超时上限**：单目标（如"进入游戏主循环"）总预算 ≤120s 帧时间；超限即停，报告当前场景签名与已试序列，请求人工决策——不要无限重试。
4. **录制互斥**：录制中不用 screenshot 步骤（自动 skipped），观察走 state_probe。
5. **断点互斥**：设了断点的会话玩不动（命中即暂停）——游玩前确认无遗留断点（`breakpoint(list)`）。

## 宏库（确定性复放）

首次探索成功后，把序列录制成宏，回归场景直接复放：

- 录制：`ppsspp_replay(begin)` → 按标准节拍推进 → `ppsspp_replay(save, file_path="<场景>_<目的>.ppr", session_note="...")`
- 复放：`ppsspp_replay(load, file_path=...)` → `wait_complete` → `check_cpu_state`/probe 验证到达
- 命名约定：**`<场景>_<目的>.ppr`**（如 `title_skip_intro.ppr`、`menu_enter_newgame.ppr`），落盘 `.ppsspp-dfx/output/replays/`
- 宏前先 `replay(status)` 确认 IDLE；宏内容会随游戏版本漂移——复放后必须校验 game_mode 签名，不符则重录
- 注意：跨 PPSSPP 进程完全确定性需相同初始 RAM（save state），当前协议不支持——宏默认"从同一起点复放"（冷启动后）

## 错误处理

| 症状 | 原因 | 修复 |
|------|------|------|
| 按键无反应 | 画面不吃输入（加载/过场）或 duration 单位错 | 见判定信号；duration 是帧（C1） |
| 菉单跳到错误项 | 上一次按键的重复生效/节奏太快 | 加大等待帧；单轮单按键 |
| 对话推进后回退 | attract/空闲超时（长时间无输入） | 保持节拍连续；batch 轮间不留长空档 |
| `SESSION_BUSY` | batch/wait 占锁 | 等待完成；缩短 batch 轮数 |
| 进程消失 | 项目已知转换期不稳定段 | 查 `DUMP/log.txt` 与 `*.ppdmp`；转 crash_analysis |

## 关联资源

- 批量编排细节: [batch_automation.md](batch_automation.md)
- CPU 状态: [../../cpu-state-contract.md](../../cpu-state-contract.md)
- 项目侧界面签名与标准序列：由项目专属 skill 提供（不在本仓）
- 录制回放: [replay_recording.md](replay_recording.md)
- Smoke 前置: [smoke_test.md](smoke_test.md)
