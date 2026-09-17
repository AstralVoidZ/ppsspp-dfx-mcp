---
title: MCP 工具面速查
type: references
category: tools
---

# MCP 工具面速查（静态工具 + 动态脚本工具）

> 权威来源是 MCP `tools/list`；本表补充各工具的**防护语义与前置条件**，供调用前确认。工具总数以 `tools/list` 为准，本文不硬编码数量。高频五工具（`read_memory` / `disassemble` / `get_pc` / `step` / `screenshot`）的 `session_id` 可省略（唯一活跃会话自动解析；0 会话报错提示启动，多会话报 `SESSION_AMBIGUOUS` 并列出全部 id）。CPU 状态列含义见 [cpu-state-contract.md](cpu-state-contract.md)（ANY=随时可用 / RUN=必须运行中 / STEP=内部自动暂停恢复 / PAUSE=调用方须先暂停）。
>
> ⚠️ **地址参数必须写全 `"0x"` 前缀**——裸数字串按十进制静默解析为错误地址且调用成功返回（规则全文见 [ppsspp-constraints.md](ppsspp-constraints.md) §C6）。

## 会话与健康

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_health` | 探测 server 存活 | 不连 PPSSPP；`tool_count` 可作自检（静态数+暴露脚本数，以 `tools/list` 为准） |
| `ppsspp_session` | start/stop/get/wait_ready | start 需 `iso_path`，`wait_ready=true` 一步等就绪（默认 75s 预算，`[BOOT_TIMEOUT]`=楔死嫌疑，勿继续重试读；等价旧 start→wait_ready 两步）；`start(resilient=true)` 自愈启动：楔死→隔离 GPU 黑名单→同 id 重启 ≤2，`recovered>0` = 现场已重置；错误码 `ISO_NOT_FOUND`/`PPSSPP_NOT_FOUND`/`PORT_CONFLICT` |
| `ppsspp_session(action="list")` | 列会话 | 空闲 30min 会话被 GC |
| `ppsspp_smoke_test` | 四项健康检查 | `iso_loaded/cpu_running/ws_connected/game_mode_valid`；game_mode_valid 依赖 addresses.yaml 配置 |

## 内存

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_read_memory` | bytes/u32/string/scan | read_bytes ≤65536，`output`=value（默认，字节列表+hex text）/hex（只回 hex text，`value=null`）/file（落盘 `output/memory_reads/` 回路径+64B 预览，顶格读取必用）；read_string 内部即 read_bytes+找 NUL（max_len 默认 4096，解码触顶 `truncated=true`）；scan 区间 ≤256MiB、pattern ≤4096B、不可读块静默跳过、支持 hex/ascii |
| `ppsspp_write_memory` | u8/u16/u32/bytes | DESTRUCTIVE；受保护区需 `force=True`（`PROTECTED_ADDRESS`）；bytes 接受 hex 或 base64 |

## 断点

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_breakpoint` | set/remove/list/update + mem_set/mem_remove/mem_list/mem_update | 设断可 ANY 态；**可靠命中需 CPUCore=2**；CPU 断点 set/remove 后 PPSSPP 无返回，工具自动跟 list 回填；内存断点按 address+size 匹配（remove 先 list 解析真实 size），size=1/2/4 为定宽监视、更大值按区间透传给 PPSSPP；`mem_update` 无条件合并 read/write/change（PPSSPP 可选 bool 缺省=false，漏传会清零）；`change=true` 是"值变化"断点；`mem_list` 的 `hits` 计数可确认命中 |
| `ppsspp_frame_snapshot` | 一步暂停抓现场（pc+寄存器+可选探针→恢复） | 整体占锁（短）；运行态来的调用结束前恢复，已暂停的保持暂停；采集失败也不会留下冻结 |
| `ppsspp_wait_breakpoint` | 阻塞等断点命中（替代 gpu_stats 探针循环） | 先 `breakpoint` 设防再调用；消费 `cpu.stepping` 广播，命中返回 `{hit, pc, reason, related_address, ticks}`，超时返回 `hit=false`（**非错误**，可轮询）；入口即已暂停→`hit=true, already_paused=true`；等待期**不占会话锁**（可并发读/观察，勿发 step/pause/resume） |
| `ppsspp_trace_memory_access` | 一步定位"谁在读/写此地址" | 设防→等命中→抓 pc/寄存器/回溯→清除断点→恢复运行一次完成；游戏须 RUN（已暂停→`already_paused=true` 不设防）；`access=read/write/read_write`，size∈{1,2,4}；等待期不占锁，勿并发其他断点/step 操作（首个广播胜出，无论来自谁）；异常路径仍保证清除断点 |

断点工作流见 [cpu-state-contract.md](cpu-state-contract.md) §断点命中协议。

## 步进与运行态

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_step` | into/over/out/pause/resume/reset/run_until/next_hle | into/over/out 内部保证暂停态；**stepInto 首次调用只暂停不步进**；over/out/run_until 靠临时断点，永不到达→超时是预期；3 次无推进→`STEP_NO_ADVANCE`；reset 重启游戏丢全部内存态 |
| `ppsspp_get_pc` | 安全读 PC | STEP（自动暂停-恢复，trust HIGH；已暂停时保持不恢复） |

## 查询

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_query` | game_state/registers/register/backtrace/threads/modules/funcs/func_scan/func_add/func_remove | `backtrace`/`func_*` 需 PAUSE（运行态调→`CPU_STATE_ERROR`）；RUNNING 态 `registers` 的 PC 为 LOW trust；`hle.func.list` 可达 700+KB，`top_n` 默认 100 截断（传 0 取全量）；`func_remove` 只收 address |

## 寄存器与表达式

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_write_register` | 写 GPR/FPU/VFPU/pc/hi/lo | STEP（自动暂停恢复）；DESTRUCTIVE；ABI 名 |
| `ppsspp_evaluate` | 调试表达式求值 | STEP；**不支持 `*addr` 解引用**（用 read_u32）；返回 `{EXPR} = 0xVAL` |

## 反汇编与汇编

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_disassemble` | 反汇编 N 条 | count ≤100（静默截断）；count≤0 返回空 |
| `ppsspp_search_disasm` | 反汇编文本搜索 | `$` 前缀自动剥除；end=0 循环搜索；每命中补全 disasm 文本 |
| `ppsspp_assemble` | MIPS 汇编写入 | DESTRUCTIVE；PPSSPP 只汇编一条→工具按 `\n`/`;` 拆分逐条（注意 armips 语法里 `;` 是注释）；受保护区检查按总长 |

## 输入与等待

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_press_button` | 单键 press | duration 单位帧（60fps），上限 18000；按钮名 25 项白名单 |
| `ppsspp_hold_buttons` | 按住组合键 | 持久态直到下次调用；空串=释放全部 |
| `ppsspp_send_analog` | 摇杆 | PSP 原生 [0,255]，128 居中 |
| `ppsspp_wait_frames` | 等待 N 帧 | 上限 18000；`interval` ∈[0.0001,1.0]s；块间校验会话存活 |

## 截图与 GPU

> **图像工具返回两个通道**（`ppsspp_screenshot` / `ppsspp_dump`，kind=`texture`/`clut`）：像素在 `content` 的 **ImageContent** 块，元数据在 **`structuredContent`**——**没有**文本块承载元数据，`image_base64` 也**不在**结构化通道里。三者都会**自动落盘**，元数据里的 `file_path` 指向已保存的文件：要再用这张图，读那个文件即可，**不要重复截图**。

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_screenshot` | 抓帧缓冲 | 默认 `source=render`（空帧自动回退 VRAM，label `render→vram_fallback`，颜色不可靠）；`source=output` CRASH-RISK 勿用；与弃用 `mode` 互斥；空捕获返回 `empty:true` 不报错（`structuredContent` 含 `mode/source/file_path/size_bytes/width/height/format/empty`） |
| `ppsspp_dump(kind=...)` | 抓当前绑定纹理/CLUT（v0.1.6 合并）| 只能抓"当前绑定"，不支持按 VRAM 地址；空捕获报 `CAPTURE_EMPTY`；`structuredContent` 只含元数据（texture 含 `level`），像素走 ImageContent |
| `ppsspp_gpu_stats` | fps/vblanks/info | **RUN**（暂停时 `CPU_STATE_ERROR`）——可反向用作"CPU 是否暂停"的探针 |
| `ppsspp_gpu_record` | 抓下一帧 GE 命令流 | RUN；二进制落盘 `output/gpu_dumps/`，不进 JSON |

## 日志与地址

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_analyze_log` | 过滤 ERROR/WARNING/CRASH | 默认读广播日志镜像 `.ppsspp-dfx/output/ppsspp.log`；log_path 白名单限 `.ppsspp-dfx` 树内；>10MiB 拒绝；匹配上限 500 |
| （`ppsspp_convert_address` 已非工具化）| IDA↔PPSSPP 换算 = 纯算术 | 偏移 = `top_base.ppsspp - top_base.ida`；IDA 偏移→运行时用 `ida_to_ppsspp` |
| `ppsspp_list_addresses` | 列 addresses.yaml 常量 | ≥0x1000 的 int 输出 hex 串，可直接回填地址参数；未知 section 报错并列出有效 section |

## 编排与观察

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_batch_step` | press/wait/state_probe/screenshot 序列编排 | 有失败步时整体 isError（`BATCH_STEP_FAILED`），按 `results[]` 排查；录制中 screenshot 步自动 skipped（非失败） |
| `ppsspp_batch_status(batch_id 省略)` | 列举全部在册后台批任务（找回丢失的 batch_id / 背景活动盘点；v0.1.6 合并）| 只读无锁；按提交序返回；完结任务仅保留最近 32 个（`retention_jobs`），更早的已被淘汰 |
| `ppsspp_batch_status` | 轮询后台批任务状态与进度（completed 携带完整 results[]） | 只读无锁，可与读工具并发；永不触碰会话锁 |
| `ppsspp_batch_cancel` | 取消排队中/运行中的后台批任务 | 取消发生在当前步边界；取消已完结任务是错误——不确定状态先 batch_status
| `ppsspp_state_observer` | 命名探针注册/观察 | observe 在 RUNNING 态可靠；注册表**进程级**共享（跨会话），`register` 不写回 YAML，`clear` 后同进程不再播种 |
| `ppsspp_replay` | 录制/回放 10 动作 | 录制需 RUN（真实输入时序）；`save`/`load` 的 `file_path` 只允许裸文件名（恒落 `output/replays/`）；`execute` 不自动结束，必须 `wait_complete`；录制中禁 screenshot、read_uN 可用 |

## 内存映射元数据

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_memory_map` | 区域映射表 | 只读 |
| `ppsspp_search_memory_info` | 按标签搜分配元数据 | match 必填（大小写不敏感子串）；返回单个 extent |

## 脚本系统

| 工具 | 用途 | 关键约束 |
|------|------|---------|
| `ppsspp_list_scripts` | 列 manifest 脚本 | `status` 字段区分可用性：`migrated`=可运行，`skeleton`=未实现（调用返回 not_implemented，勿当可用能力）；exposed 条目带 `exposed_registered` 实际注册态 |
| `ppsspp_run_script` | 按名执行 | Input 经 Pydantic 校验；未知名→`SCRIPT_NOT_FOUND`；`requires_ppsspp=true` 的脚本无会话时直接报 `SESSION_NOT_FOUND`（工具层强制，优先级：参数 > Input 字段） |
| `ppsspp_reload_scripts` | 重读 manifest + 清缓存 + 同步动态工具 | 幂等；会注册/注销 `ppsspp_script_*` 使其与 manifest 一致（无需重启），返回 `exposed_added`/`exposed_removed` 对账。**工具集真的变了时，请自己重取一次 `tools/list`**：server 的 `capabilities.tools.list_changed` 恒为 `false`（SDK 入口不可达，见下），下发的通知是尽力而为的额外项，不能当作缓存失效的保证 |

## Resources、Prompts 与补全

- `ppsspp://game-state` / `ppsspp://registers` — 只读快照，仅单活跃会话时可用（0 或多会话报 ResourceError，改用带 session_id 的工具）。
- prompt `memory-breakpoint-wizard(address, size, purpose)` — 渲染六步内存断点工作流文本（前置检查→记录当前值→mem_set→resume→命中分析→mem_remove），可作操作清单。
- prompt `memory-trace-wizard(address, purpose)` — 渲染"谁在读/写此地址"追踪工作流：首选 `ppsspp_trace_memory_access` 一次调用，降级为 mem_set+wait_breakpoint 手动协议；含命中现场解读（PC 停在访问指令之后、部分构建 reason/related_address 为空）。
- 两个 prompt 的 `address` 参数支持**补全**（服务端声明了 `completions` 能力）：候选来自 `.ppsspp-dfx/config/addresses.yaml` 的**运行时地址**，前缀匹配，可省略前导零（`8804` 能命中 `0x08804000`）。候选只返回十六进制地址串、不返回符号名——因为它就是将被写入参数的**值**。

> **能力面事实**（握手声明）：server 声明 `tools` / `resources` / `prompts` / `completions` 四项；`logging`（协议已移除）与 `tasks`（SDK 仅有类型定义）不声明。`tools.list_changed` 与 `resources.subscribe` **恒为 `false` 且是有意为之**——SDK 2.2.0 的 `MCPServer` 未提供握手法入口，声明等于承诺一个发不出的通知。

## 关联资源

- CPU 状态契约与断点命中协议: [cpu-state-contract.md](cpu-state-contract.md)
- 错误码恢复全表: [error-codes.md](error-codes.md)
- 协议层事实: [ppsspp-constraints.md](ppsspp-constraints.md)
