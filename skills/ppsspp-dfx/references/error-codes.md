---
title: 错误码恢复全表
type: references
category: errors
---

# 错误码恢复全表

> Server 侧全部错误码（errors.py 异常分类 + `PROTECTED_ADDRESS` + `INTERNAL`）的语义与恢复路径。本表是 SKILL §5 高频症状矩阵的补全：矩阵收"出现频率高/易误判"的条目，本表收全部。恢复动词均可在工具描述与错误 message 中得到印证；若 message 与本表冲突，以 `tools/list` 描述与实际 message 为准并上报 drift。

## 会话生命周期

| 错误码 | 语义 | 恢复路径 |
|---|---|---|
| `SESSION_NOT_FOUND` | 会话 ID 不在活跃表 | `ppsspp_session_list` 查看；不存在则 `session(action="start")` 重建 |
| `SESSION_EXPIRED` | 会话进程已死 | `session(action="start")` 重建（勿复用旧 session_id） |
| `SESSION_BUSY` | 另一工具调用正占会话锁（>5s 报出） | 等当前长操作完成再调；`wait_breakpoint`/`trace_memory_access` 等待期不占锁，可并发读/观察 |
| `SESSION_ALREADY_EXISTS` | 同资源已有活跃会话 | `session_list` 找到现有会话直接复用，或先 `session(action="stop")` 再建 |
| `SESSION_AMBIGUOUS` | 省略 `session_id` 时有多个活跃会话（高频五工具自动解析的歧义保护） | 按 message 列出的 id 显式传入目标会话，或 stop 多余会话 |
| `BOOT_TIMEOUT` | CPU 未在启动预算内就绪（楔死嫌疑） | `ppsspp_analyze_log` 查启动错误（GPU backend 失败是已知根因）→ `session(stop)` → `start(resilient=true)` 重试 |

## 启动与配置

| 错误码 | 语义 | 恢复路径 |
|---|---|---|
| `ISO_NOT_FOUND` | ISO 文件不存在 | 用存在的绝对路径重试 `session(start)` |
| `PPSSPP_NOT_FOUND` | PPSSPP 可执行文件路径无效 | 检查 `ppsspp_exe` 配置（project.yaml 或 `PPSSPP_DFX_EXE_PATH`）后重试 |
| `PORT_CONFLICT` | WS 端口被另一活跃会话占用 | `session_list` 找占用者并 stop；或换 `PPSSPP_DFX_WS_PORT` |
| `CONFIG_INVALID` | 启动配置校验失败 | 按 message 修 `.ppsspp-dfx/config/` 配置或环境变量，重启 server |

## 连接与协议

| 错误码 | 语义 | 恢复路径 |
|---|---|---|
| `WS_CONNECT_FAILED` | WS 连接建立失败 | 确认 PPSSPP 已带 debugger 参数启动且端口一致；`session(get)` 查健康或重建会话 |
| `WS_TIMEOUT` | WS 调用超时（PID 状态未知，保守处理） | **勿直接判定死亡**：`session(get)` 查健康后再决定重试或重建 |
| `WS_DISCONNECTED` | 连接丢失或进程死亡 | `session(action="start")` 重建 |
| `PPSSPP_PROTOCOL_ERROR` | PPSSPP 对 WS 调用返回错误事件 | 按 message 中 PPSSPP 侧错误信息修正请求参数后重试 |
| `RATE_LIMIT_EXCEEDED` | 超过每分钟工具调用上限 | 退避后重试；把循环类调用合并进 `batch_step` 一次提交 |

## CPU 状态与步进

| 错误码 | 语义 | 恢复路径 |
|---|---|---|
| `CPU_STATE_ERROR` | CPU 状态前置不满足（如 PAUSE 态调需 RUN 的查询） | 按工具的 CPU 状态列（tool-surface）先 `step(action="pause")` 或 `resume`；见 [cpu-state-contract.md](cpu-state-contract.md) |
| `CPU_FREEZE_SUSPECTED` | PID 存活但 CPU 不进入/不推进 | **勿 stop 重启会话**：`screenshot` 拍现场 → `step(action="resume")` 尝试解冻 → `query(action="threads")` → `disassemble` 看 PC 指令流 |
| `STEP_NO_ADVANCE` | 步进命令被消费但 CPU 无推进（×3） | 确认 CPUCore=2（IR Interpreter）；`over`/`out`/`run_until` 靠临时断点，目标不可达时改用 `breakpoint`+`wait_breakpoint` |
| `STEP_OUT_ERROR` | step_out 返回无效结果 | 改用 `step(action="over")` 或对已知返回地址 `run_until` |
| `IR_ENCODING_DETECTED` | `read_u32` 读到 JIT-IR 编码（0x68xxxxxx） | 代码段一律用 `disassemble` / `search_disasm`，勿用 read_uN 读 |

## 内存与断点

| 错误码 | 语义 | 恢复路径 |
|---|---|---|
| `PROTECTED_ADDRESS` | 写入受保护区（kernel <0x08800000、top.prx 代码段） | 确认意图后加 `force=True`；误写会崩溃，先 `convert_address` 核对目标 |
| `BREAKPOINT_ERROR` | 断点操作失败 | 确认 CPUCore=2 与地址可执行；`mem_remove` 前先 `mem_list` 解析真实 size（按 address+size 匹配） |
| `ADDR_INVALID` | 地址格式或范围非法 | 用 `"0x"` 前缀 hex 字符串重试；范围常量查 `ppsspp_list_addresses`，区域查 `ppsspp_memory_map` |
| `SCAN_NO_MATCH` | 内存扫描无命中 | 放宽 pattern、扩大 start_addr/end_addr、核对字节序；可用 `read_bytes` 抽查目标区域佐证 |
| `VERIFY_MISMATCH` | 反汇编结果与预期指令不符 | `ppsspp_convert_address` 核对 IDA↔运行时换算；确认补丁/hook 是否真正生效 |

## 脚本系统与其它

| 错误码 | 语义 | 恢复路径 |
|---|---|---|
| `SCRIPT_NOT_FOUND` | manifest 中无此脚本名 | `ppsspp_list_scripts` 查可用名（注意 `[skeleton]` 条目不可用） |
| `SCRIPT_CONTRACT_ERROR` | 脚本 Input/Output/entry 契约损坏 | 查 manifest 条目与脚本模型定义；可经 `ppsspp_run_script` 获取更清晰的错误面 |
| `MANIFEST_ERROR` | scripts.manifest.yaml 损坏或缺引用 | 修复 YAML 后 `ppsspp_reload_scripts`（无需重启 server） |
| `ARGS_INVALID` | 工具参数未通过 schema 之外的校验（范围/跨字段/文件内容约束） | 按 message 修正参数后重试；涉及地址先 `ppsspp_list_addresses` 核对 |
| `STEP_INVALID` | batch_step 的 step 结构非法（type/button/frames 越界） | 按 message 定位 `step[index]` 修正后重试 |
| `NOT_IMPLEMENTED` | 请求动作未实现（skeleton 脚本等） | `list_scripts` 确认 status；不要把 skeleton 当可用能力 |
| `INTERNAL` | 服务器内部故障（兜底码；参数校验已由 `ARGS_INVALID` 承担） | 重试一次；持续复现按 §7 逃生舱走一次性 WS 客户端取证 |

## 关联资源

- 高频症状矩阵（按键无效、截图空帧等非错误码症状）: SKILL.md §5
- CPU 状态契约: [cpu-state-contract.md](cpu-state-contract.md)
- 协议层事实: [ppsspp-constraints.md](ppsspp-constraints.md)
