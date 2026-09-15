---
title: 崩溃与卡死根因分析
type: playbook
scope: common
category: crash-analysis
---

# Playbook: 崩溃与卡死根因分析

> 通用 PPSSPP 项目崩溃/卡死分析 playbook，跨 PSP 项目可复用。项目特定崩溃模式（如 callback 未初始化蓝屏）见项目专属 playbook。

## 何时使用

- PPSSPP 报 "Bad Execution Address" / "Bad memory access"
- 游戏运行中黑屏、卡死、画面冻结
- smoke test FAIL 但原因不明
- 需要崩溃时的线程状态与调用栈

## 判定信号

| 崩溃模式 | 判定信号 | 通用根因 |
|----------|---------|---------|
| 空指针解引用 | `Bad memory access`，崩溃地址接近 0 | 全局/静态指针未初始化；查模块加载顺序 |
| 除零异常 | MIPS `div` 附近崩溃 | 除数未初始化或为 0 |
| 栈溢出 | `$sp` 越界、`$ra` 为垃圾值 | 递归过深或栈帧过大 |
| 无限循环/卡死 | `CPU_FREEZE_SUSPECTED`、多次采样同一 PC | 循环条件恒真、状态机无出口、HLE 阻塞 |
| 函数指针跳垃圾地址 | `Bad Execution Address` + `0x0E0E0E0E` 风格地址 | 回调/函数指针未初始化即被调用（可用 `ppsspp_script_find_0e_source` 追踪来源） |
| MIPS 无效指令 | 反汇编结果不是有效 MIPS 指令 | 代码段被破坏（hook 写错地址 / 补丁错误） |
| `isCurrent=idle0` | 用户线程全部 WAIT/SUSPEND/DORMANT | 不一定是卡死（constraints A8）；按 §3 线程流程多次确认 |

## 调用顺序

1. **离线日志先行** — `ppsspp_analyze_log()` 扫镜像日志；进程已死时读 PPSSPP 文件日志 `memstick/PSP/SYSTEM/DUMP/log.txt` 与 `*.ppdmp` 转储（GPU 崩溃），提取崩溃地址/线程名/异常类型。
2. **现场留存（进程还活着）** — `CPU_FREEZE_SUSPECTED` 时按 [../../cpu-state-contract.md](../../cpu-state-contract.md) §5：`ppsspp_screenshot` 拍现场 → **不要 stop 会话**。
3. **暂停并取上下文** — `ppsspp_step(action="pause")` → `ppsspp_get_pc`（HIGH trust）→ `ppsspp_query(action="registers")` → `ppsspp_query(action="threads")` → `ppsspp_query(action="backtrace")`（调用链，比 PC 采样可靠，见 constraints C5）。
4. **反汇编崩溃点** — `ppsspp_disassemble(address=<崩溃PC>, count=16)`。若得到 `0x68xxxxxx` 序列说明误用了 read_u32（constraints A1）。
5. **崩溃地址归因** — 用 `ppsspp_convert_address(mode="ppsspp_to_ida")` 把运行时地址换算回 IDA 地址，对照 addresses.yaml `known_functions` 定位函数。
6. **恢复或收尾** — 分析完 `ppsspp_step(action="resume")`；进程已死则 `ppsspp_session(action="stop")` 后重建。

## 线程类型识别

- `user_main` — 主线程，崩溃通常在此
- `idle0`/`idle1` — 内核空闲线程，`isCurrent=idle0` 非卡死
- 项目特定工作线程 — 对照 addresses.yaml 或项目 playbook（如项目的 `startup_logos_thread` 工作线程）

## 错误处理

| 症状 | 原因 | 修复 |
|------|------|------|
| `backtrace` 报 `CPU_STATE_ERROR` | 未暂停 | 先 `step(action="pause")`（constraints B1） |
| 崩溃点反汇编为 0x68xxxxxx | 读了代码段 IR 编码 | 用 `disassemble`（constraints A1） |
| 寄存器大面积 `0xDEADBEEF` | 线程处于内核等待、用户寄存器未物化 | 结合 `query(threads)` 状态与 `backtrace` 判断，勿直接当数据解读 |
| `resume` 后仍冻结 | 真死循环 | 重复 §3 记录多次 PC 对比是否同一位置；对照判定信号"无限循环" |
| 进程消失无 `CPU_FREEZE_SUSPECTED` | PPSSPP 直接退出/崩溃 | 查 `DUMP/log.txt` 尾部与 `*.ppdmp`；按离线路径 §1 分析 |

## 关联资源

- CPU 状态契约与 FREEZE 工作流: [../../cpu-state-contract.md](../../cpu-state-contract.md)
- 协议约束: [../../ppsspp-constraints.md](../../ppsspp-constraints.md)（A1 IR 编码 / A2-A4 PC 不可信 / A8 idle0 / B1 先暂停 / B2 disasm）
- Smoke 前置: [smoke_test.md](smoke_test.md)
- 截图留证: [screenshot.md](screenshot.md)
