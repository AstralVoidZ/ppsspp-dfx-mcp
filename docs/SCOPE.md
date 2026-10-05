# SCOPE

本文件是 `ppsspp-dfx` MCP Server 的**能力边界声明**，用于对齐三个数字：
本文档的声明数、`README.md` 的声明数、以及实现的实际值。

> **维护规则**：任何新增/移除工具的改动，必须同步更新三处，并由
> `tests/unit/l2_mcp_contract/test_tool_surface_baseline.py` 门禁兜底。
> 不一致时以**实现实测值**为准，并回写本文档。

---

## 1. 工具总量

| 项 | 数量 | 核实方式 |
|---|---|---|
| **静态工具**（装饰器注册） | **37** | `server.register_all_tools()` 后 `mcp.list_tools()` |
| 动态脚本工具 | 随 `scripts.manifest.yaml` 变化 | `ppsspp_list_scripts` |
| 注册后合计（实测） | **37 + N** | 见下方说明 |

**实测命令**：

```python
import asyncio
from ppsspp_dfx_mcp import server as s

s.register_all_tools()
print(len(asyncio.run(s.mcp.list_tools())))  # -> 37
```

> ⚠️ 早期文档曾写「41 个工具」。该数字包含未在当前构建中暴露的条目，
> 已按实测更正为 **37**。

---

## 2. 静态工具清单（37）

| # | 工具 | 类别 |
|---|---|---|
| 1 | `ppsspp_analyze_log` | 诊断 |
| 2 | `ppsspp_assemble` | 代码 |
| 3 | `ppsspp_batch_cancel` | 批处理 |
| 4 | `ppsspp_batch_status` | 批处理 |
| 5 | `ppsspp_batch_step` | 批处理 |
| 6 | `ppsspp_breakpoint` | 调试 |
| 7 | `ppsspp_context` | 诊断 |
| 8 | `ppsspp_diff_memory` | 内存 |
| 9 | `ppsspp_disassemble` | 代码 |
| 10 | `ppsspp_dump` | 内存 |
| 11 | `ppsspp_evaluate` | 调试 |
| 12 | `ppsspp_frame_snapshot` | 观测 |
| 13 | `ppsspp_gpu_record` | 图形 |
| 14 | `ppsspp_gpu_stats` | 图形 |
| 15 | `ppsspp_health` | 诊断 |
| 16 | `ppsspp_hold_buttons` | 输入 |
| 17 | `ppsspp_list_addresses` | 元数据 |
| 18 | `ppsspp_list_scripts` | 元数据 |
| 19 | `ppsspp_memory_map` | 内存 |
| 20 | `ppsspp_press_button` | 输入 |
| 21 | `ppsspp_query` | 观测 |
| 22 | `ppsspp_read_memory` | 内存 |
| 23 | `ppsspp_reload_scripts` | 元数据 |
| 24 | `ppsspp_replay` | 输入 |
| 25 | `ppsspp_run_script` | 脚本 |
| 26 | `ppsspp_scan` | 内存 |
| 27 | `ppsspp_screenshot` | 图形 |
| 28 | `ppsspp_search_disasm` | 代码 |
| 29 | `ppsspp_search_memory_info` | 元数据 |
| 30 | `ppsspp_send_analog` | 输入 |
| 31 | `ppsspp_session` | 会话 |
| 32 | `ppsspp_state_observer` | 观测 |
| 33 | `ppsspp_step` | 调试 |
| 34 | `ppsspp_wait_frames` | 观测 |
| 35 | `ppsspp_watch_value` | 观测 |
| 36 | `ppsspp_write_memory` | 内存 |
| 37 | `ppsspp_write_register` | 调试 |

---

## 3. 动态脚本工具

由 `scripts/manifest.yaml` 声明，运行时以 `ppsspp_script_<name>` 暴露。
数量随脚本增减而变，**不在本文档硬编码**，请用 `ppsspp_list_scripts` 查询。

---

## 4. 已知能力边界

以下限制是**实测确认**的，不是推测。使用前请知悉：

### 4.1 地址知识库是 TOPX 专属

`ppsspp_context` 的 `identity` / `region` 字段来自 `.ppsspp-dfx/config/addresses.yaml`，
其中的函数名与模块布局**只覆盖 TOPX 的目标镜像**。

对其他 ISO，这两个字段返回 `identity: null` / `region: ""`，此时 `note`
会明确写明「unknown / not applicable」——**这不表示地址无效**。

### 4.2 状态探针地址已失效

`state_probes` 中的四个地址（`game_mode` / `cursor_x` / `cursor_y` /
`prim_counter`）经 2026-10-02 实测**全部失效**，其中三个根本不在 `top.prx`
跨度内。地址值保留供日后重新定位作输入，但**当前不可依赖**。

「是否已进入游戏」可改用已验证稳定的替代判据（函数指针表是否被填充，
或 5 个稳定小整数变量），但**尚未接入工具**。

### 4.3 `gpu.stats.*` 依赖模拟器在渲染

`ppsspp_gpu_stats` / `ppsspp_gpu_record` 是 **ticketed** 调用，由 PPSSPP 在
**下一次 GPU 翻转**时响应。CPU 暂停或模拟器被阻塞时不会返回。

超时会报出**归因成因**（`expected_stall` / `pairing_broken` /
`no_producer` / `transport_error`），而不是只说「超时」。

### 4.4 写保护边界

`ppsspp_write_memory` 对 kernel 区、VRAM、暂存区与 `top.prx` 代码段有保护，
需 `force=true`。保护范围由**运行时模块信息**计算，非硬编码常量。

---

## 5. 三处数字的一致性检查

| 来源 | 声明值 |
|---|---|
| 本文档 §1 | 37 |
| `README.md` | 37 |
| 实现实测 | 37 |

**检查方法**：

```powershell
$env:PYTHONPATH = "src"
python -c @"
import asyncio
from ppsspp_dfx_mcp import server as s
s.register_all_tools()
print(len(asyncio.run(s.mcp.list_tools())))
"@
```

工具描述与 schema 的变更由 `tests/unit/l2_mcp_contract/test_tool_surface_baseline.py`
强制门禁：描述或输入 schema 变化会让该测试失败，必须走 sanctioned 路径
（`scripts/dump_tool_surface.py`）重新生成基线并复核 diff。

## 6. 危险性注解口径

单一来源：`src/ppsspp_dfx_mcp/tools/_common.py` 的 `DESTRUCTIVE_HINT_POLICY`。

> **破坏性 = 不可逆地改变被测目标的状态。** 清除工具自身的调试记账（断点、已注册探针）**不构成**破坏性——它不触碰被测目标状态，且可重新注册恢复。

| 工具 | `destructiveHint` | 理由 |
|------|:---:|------|
| `ppsspp_assemble` / `ppsspp_write_memory` / `ppsspp_write_register` | `true` | 写入被测目标地址空间 / CPU 寄存器 |
| `ppsspp_breakpoint`（remove / mem_remove） | `false` | 删的是调试记账 |
| `ppsspp_step`（reset） | `false` | 不改目标内存 |
| `ppsspp_state_observer`（clear） | `false` | 清的是已注册探针 |
| `ppsspp_diff_memory`（drop） | `false` | 丢的是工具自拍的快照 |
| `ppsspp_replay`（abort）/ `ppsspp_batch_cancel` | `false` | 停的是工具自建的播放/队列 |

守门：`tests/unit/l1_contract/test_annotation_policy.py` 逐工具断言四个 hint 均为显式布尔，
且**含移除/复位语义的工具必须有口径归属**（未登记即失败）。

## 7. 条件必填参数

`address` / `version` / `value` 在 schema 里带哨兵默认值（`"0x0"` / `0`）但不在 `required`
内——通用 JSON-Schema 客户端会静默填默认值。schema 默认值**不能删**（会破坏既有调用方），
故补救在描述层：这类参数的描述**首句**必须写明何时必填。

登记表：`tools/_common.CONDITIONAL_REQUIRED_PARAMS`（7 处）。守门断言：登记项存在、
确实不在 `required`、确实带非 null 默认、描述首句含 `Required`，且 `size` 类**不得**误入登记表。

## 8. 守门分组与预算

契约守门**物理分为两组**：

| 组 | 内容 | 预算 |
|----|------|------|
| **静态组**（不启动模拟器） | S-1 契约面导出、S-2 描述完整性、S-3/S-4 注解与条件必填、S-5 判据引用、S-6 轮次对比、S-7 判定器自检、S-8 渲染一致性、S-9 协议契约、S-10 归档文本完整性 | **30 s**（软目标，按实测重定基：10 → 21.09 实测 → 30） |
| **真机组** | `verify_real_mcp.py` phase B/C、SC-009 性能统计、SC-020 探针失效感知 | 未执行时 MUST 显式声明 `SKIPPED_NOT_EXECUTED: <check> — <reason>` |

入口：仓内按 CI 顺序执行静态组全部步骤的守门脚本；产物为运行期生成的 JSON 报告。
**超出预算时调整预算本身并留痕，MUST NOT 删减检查项、放宽判据或缩小覆盖面来迁就预算。**
