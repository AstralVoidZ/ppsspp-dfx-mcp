---
title: PPSSPP 协议约束
type: references
category: protocol
protocol_group: [A, B, C]
---

# PPSSPP 协议约束

> Agent 调用 PPSSPP 调试工具时必须遵守的协议层约束，按"协议层事实 / 调用顺序约束 / 工具行为约束"三组组织。工具签名见 [tool-surface.md](tool-surface.md)；CPU 状态判定见 [cpu-state-contract.md](cpu-state-contract.md)；项目上下文由项目专属 skill 提供（不在本仓）。
>
> 地址常量（如 `text_render_dispatcher`、`game_mode` 的地址值）由 `.ppsspp-dfx/config/addresses.yaml` 作为唯一来源维护——本文件不写地址值。

## A. 协议层事实（PPSSPP 源码层，MCP 无法暴露）

### A1. `read_u32` 对代码段返回 IR 编码

直接读取代码段地址会得到 IR 编码（`0x68xxxxxx`），不是真实 MIPS 指令。

- **正确做法**：`ppsspp_disassemble` 反汇编代码段；MCP 可能报 `IR_ENCODING_DETECTED`

### A2. RUNNING 态 PC 被 VBlank 中断误导

`cpu.status.pc` 在 RUNNING 状态可能命中 `0x08800000-0x08804000` 区域（VBlank 处理位置）。PPSSPP 源码明文标注 "pc: inaccurate unless stepping"（`CPUCoreSubscriber.cpp:105`）。

- **正确做法**：判断游戏状态用 `game_mode` 变量（地址查 addresses.yaml，经 `ppsspp_state_observer` 或 `read_memory` 读取）；查 PC 用 `ppsspp_get_pc`（内部暂停-恢复，trust HIGH）

### A3. `hle.thread.list.isCurrent` 在 RUNNING 态不可信

`isCurrent` 仅反映 HLE 调度器上次切换的线程，JIT/IR 执行 native 代码时不更新。

- **正确做法**：先 `ppsspp_step(action="pause")` 再 `ppsspp_query(action="threads")`
- **判定**：`isCurrent=idle0` 不代表卡死（idle0 是内核空闲线程，见 A8）

### A4. RUNNING 态裸寄存器查询为 LOW trust

`currentMIPS->pc` 仅在调度点同步，可能落后数千条指令。

- **正确做法**：`ppsspp_get_pc` / 暂停后 `ppsspp_query(action="registers")`

### A5. `game.status.paused` ≠ CPU stepping

`paused` 是 UI 暂停菜单态；断点命中的 stepping **不会**改变它。CPU 暂停态的判定见 [cpu-state-contract.md](cpu-state-contract.md) §3.2。

### A6. `cpu.stepping` 广播是可靠暂停信号

收到该广播后所有 CPU/线程查询可信。MCP 层的 `get_pc`、`write_register`、`evaluate` 已内部消费该信号。

### A7. 线程修改类操作强制要求 stepping

`hle.thread.wake`/`hle.thread.stop`（`ppsspp_query` 未暴露的部分经脚本走 WS）在未暂停时被 PPSSPP 拒绝（`HLESubscriber.cpp:107-110`）。

### A8. idle0/idle1 是 PSP 内核正常空闲线程

`isCurrent=idle0` 仅表示用户线程全部 WAIT/SUSPEND/DORMANT，并非卡死。真实卡死判定走 `CPU_FREEZE_SUSPECTED` 工作流（[cpu-state-contract.md](cpu-state-contract.md) §5）。

### A9. TrustLevel 三级

HIGH=stepping 后查询；MEDIUM=内存变量；LOW=RUNNING 态裸查询。详见 [cpu-state-contract.md](cpu-state-contract.md) §2。

## B. 调用顺序约束（跨工具协作）

### B1. CPU/线程/回溯查询必先暂停

`ppsspp_query(action="backtrace"/"threads"/"func_*")` 前必须 `ppsspp_step(action="pause")`，查完 `step(action="resume")`。运行态直接调会得到 `CPU_STATE_ERROR`（消息含提示）。`get_pc`/`write_register`/`evaluate` 内部已自动处理，无需手动暂停。

### B2. 读代码段必用 `ppsspp_disassemble`

不能用 `read_u32` 读代码段（A1）。搜索指令用 `ppsspp_search_disasm`。

### B3. 读多字节文本必用 `read_bytes` + decode

用 `ppsspp_read_memory(action="read_bytes")` 取原始字节后 Python 解码：
`bytes.split(b'\x00')[0].decode('shift-jis', errors='replace')`。`read_string` 仅适合纯 ASCII。

### B4. 首次 WS 连接必须发 `version` 事件

MCP 会话工具已自动处理。仅当绕过 MCP 直接用 WebSocket 客户端时需手动发送，否则 PPSSPP 等待响应超时。子协议 `debugger.ppsspp.org`。

### B5. 截图策略与降级路径

- 默认 `ppsspp_screenshot(source="render")`：暂停 CPU 取 renderColor；空帧（标题/加载屏）自动回退 VRAM 直读（label `render→vram_fallback`，颜色不可靠仅结构参考）
- `source="output"` 直接调 `gpu.buffer.screenshot`，**CRASH-RISK**（某些游戏崩 PPSSPP），仅在显式要求时用
- legacy `mode` 参数（auto=WM_COMMAND→PrintWindow→VRAM 三层 Win32 回退）已弃用，与 `source` 互斥，勿主动使用

## C. 工具行为约束

### C1. 按键 duration 单位是帧

60fps；30 帧 ≈ 0.5s。`press duration` 与 `wait frames` 上限 18000（≈300s）。

### C2. `mkisofs` 必须用 `-iso-level 4 -xa`

PPSSPP 大小写敏感路径比较；ISO 重建丢参会导致 "file does not exist: N>0"。

### C3. 断点需 IR Interpreter 模式（CPUCore=2）

JIT 模式断点不触发。配置范式见 [../assets/ppsspp.ini.template](../assets/ppsspp.ini.template)；手动启动的 PPSSPP 需在 Settings → System → CPU Core 改 IR Interpreter。命中协议见 [cpu-state-contract.md](cpu-state-contract.md) §3。

### C4. 菜单操作需完整按键序列

Start → Cross → 等待 → Cross 完整序列；单次按键常无效。推荐用 `ppsspp_batch_step` 编排。

### C5. `hle.backtrace` 比 PC 采样更可靠

调用链分析用 `ppsspp_query(action="backtrace")`（需暂停），不受 VBlank 误导。

### C6. 地址参数编码

一律 `"0x08804000"` 式 hex 字符串；返回地址以 `0x%08X` 回显，可直接回填。

⚠️ **无 `0x` 的裸数字串不会被拒绝**——解析器对无前缀输入**先试十进制**：`"08804000"` → 8804000 = `0x008656A0`。它在 32 位范围内，因此所有范围校验都会通过，调用成功返回一个**不是你想要的**地址。只有含非十进制字符的串（如 `"123abc"`）才会报 `ADDR_INVALID`。**漏掉 `0x` 是静默错误**：务必核对回显地址是否等于你传入的地址。

### C7. 寄存器名编码

只用 MIPS ABI 名（`zero/at/v0/v1/a0-a3/t0-t9/s0-s7/k0/k1/gp/sp/fp/ra`）+ `pc/hi/lo`；数字别名（`r5`）自动归一（r5→v1），`$` 前缀剥除、大小写不敏感。

### C8. 单次读取与扫描上限

`read_bytes` ≤65536；`scan` 区间 ≤256MiB、pattern ≤4096B、不可读区域静默跳过（结果为空≠内容为空，先确认区间可读）。

### C9. 受保护写区间

kernel（<0x08800000）与 top.prx 代码段（0x08804000–0x08D34000）的 `write_memory`/`assemble` 需要 `force=True`（`PROTECTED_ADDRESS`）。

## 关联资源

- CPU 状态契约与断点命中协议: [cpu-state-contract.md](cpu-state-contract.md)
- 工具签名与防护语义: [tool-surface.md](tool-surface.md)
- 项目特定上下文：由项目专属 skill 提供（启动架构 / 地址换算 / 关键函数语义）
- 配置范式: [../assets/ppsspp.ini.template](../assets/ppsspp.ini.template)
- 地址常量: `.ppsspp-dfx/config/addresses.yaml`（MCP 运行时直接读取的权威来源）
