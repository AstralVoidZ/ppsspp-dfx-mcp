# 更新日志

本项目的所有显著变更将记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.6] - 2026-09-18

### Changed（表面重构 — Glama AI 可用性评审整改：工具数 41 → 36）

净变化：8 项合并/移除 + 3 项新工具（`scan` / `diff_memory` / `context`），
41 → 36。全部能力有等价替代路径，无删减。

| v0.1.5 及之前 | v0.1.6 | 说明 |
|---|---|---|
| `ppsspp_session_list` | `ppsspp_session(action="list")` | 会话清单并入会话分发器；返回 `{sessions, count}` 不变 |
| `ppsspp_batch_list` | `ppsspp_batch_status(batch_id 省略)` | 盘点模式并入状态查询；返回 `{jobs, retention_jobs}` 不变 |
| `ppsspp_dump_texture` / `ppsspp_dump_clut` | `ppsspp_dump(kind="texture"/"clut", level?)` | 二合一；`kind="clut"` 时 `level` 必须为 0；元数据统一含 `level` 字段（clut 恒为 0） |
| `ppsspp_convert_address` | （非工具化） | 纯算术：`ppsspp_addr = ida_addr + (top_base.ppsspp - top_base.ida)`；公式移入 `ppsspp_list_addresses` 描述与技能书 `scripts/addr_convert.py` |
| `ppsspp_memory_info_search` | `ppsspp_search_memory_info` | 改名（verb_noun 约定），参数与返回不变 |
| `ppsspp_wait_breakpoint` | `ppsspp_breakpoint(action="wait")` | 严格等待（lock-free，断点保持布防），经观察者专用 step 确认通道 |
| `ppsspp_trace_memory_access` | `ppsspp_breakpoint(action="trace")` | 命中-快照-放行（必清+恢复）；仅编排内存断点（默认读访问），执行断点用 `set` + `wait` 组合 |
| `ppsspp_smoke_test` | `ppsspp_health(session_id=...)` | 四点会话电池并入健康探针：追加 `session_checks` 与 `overall_session_status`；无参形态保持零接触服务器探针语义 |
| `ppsspp_get_pc` | `ppsspp_query(action="register", name="pc", safe=?)` | `safe=true` 走 with_stepping 暂停一致性读取（≡ 原 get_pc，trust=high）；`safe=false` 裸读（trust=low，零开销热路径轮询） |

- `ppsspp_read_memory` 瘦身：`action="scan"` 迁出至新工具 `ppsspp_scan`，
  `read_memory` 回归纯读语义。
- `ppsspp_step` 瘦身：`into/over/out` 移入 `ppsspp_batch_step` 的 `cpu_step`
  步骤类型（count 1..1000 指令级批量推进）；`step` 保留
  `pause/resume/reset/run_until/next_hle` 运行控制语义。
- `diff_memory` / `scan` 的会话解析统一为**先解析会话、后校验参数**的优先序
  （两工具在"参数错 + 无会话"并发场景抛出一致的会话类错误）。
- 命名成文约定落盘 CONTRIBUTING（原子工具 verb_noun / 分发器 noun(action=) /
  族内自洽）。
- 重叠簇交叉引用：`query`/`frame_snapshot`/`state_observer`/`breakpoint`/
  `step`/`batch_step` 等描述新增 ROUTING 节（何时用我、何时改用哪个相邻工具）。
- 真机验证结论（ISO 实测）：MCP 全链路 WS 往返中位 16.7ms（n=50，经
  Inspector 客户端→服务器→PPSSPP）；WS 客户端层轻量往返 p50 0.21ms（n=60，
  localhost，见 README 性能参考）；MCP 通道回传 64KB 字节列表 ~204ms/块
  （全频段扫描必须后台化且工具内联匹配）；cpu.stepping 广播不含断点标识
  （命中归因客户端 pc↔布防表）；PPSSPP 原生 condition/log 断点字段可用。

### Added

- `ppsspp_scan`：三模式统一扫描器——`mode="pattern"`（字节模式搜索，从
  read_memory 迁入）/ `mode="value"`（Cheat-Engine 式值扫描+窄化会话，
  initial→narrow→list→drop，width u8/u16/u32，op eq/ne/lt/gt）/
  `mode="strings"`（charset 感知字符串采集：shift_jis/utf8/ascii +
  min_len + CJK 占比质量过滤）。`background=true` 提交 detached 后台作业
  （复用 batch_jobs 注册表），value initial 上限抬升至 32 MiB。
- `ppsspp_diff_memory`：内存快照差分——snapshot（64KB 分块读，单快照
  8 MiB，注册表 4 FIFO）→ compare（变更字节清单，内联 256 + truncated）→
  drop/list；纯客户端编排零新 WS 事件；不可读区段式跳过。注册表为进程级
  共享：并行会话共用同一容量与 FIFO 序。
- `ppsspp_context`：崩溃归因上下文包——known_functions IDA 偏移换算身份 +
  反汇编窗 + 可选回溯（with_stepping）。
- `ppsspp_batch_step` 新增 `cpu_step` 步骤类型：
  `{type:"cpu_step", mode:"into"|"over"|"out", count:1..1000}`。
- `ppsspp_breakpoint` 新增 `action="stats"`：窗口内命中频率统计（按 pc
  聚合）+ 探针值变化采样。
- 工具面基线锁定（`tool_surface_baseline.json` + L2 契约测试）：36 工具的
  描述与 schema 逐字节锁定，`scripts/dump_tool_surface.py` 再生成。
- README「性能参考（本机实测）」与 `docs/ppsspp-build.md`「行为契约的
  验证基线」（PPSSPP v1.20.4-605 实测口径）。
- sync 脚本 `--check` 只读比对模式（真源 HEAD ↔ 发布仓漂移检测）。
- pytest 进入 `[dependency-groups]` dev（uv 默认安装），杜绝
  `uv run pytest` 静默回落系统 PATH 旧版 pytest 的假失败。

### Fixed

- `ppsspp_scan` 后台提交返回键统一为 `batch_id`（此前 `job_id` 与
  `ppsspp_batch_status(batch_id=...)` 的参数名互相矛盾）。
- `ppsspp_breakpoint(action='trace')` 描述更正：仅编排内存断点
  （此前声称 "exec via address only"，实现中并无 exec 路径）。
- `ppsspp_health(session_id=...)` 的会话电池自包含化：`run_smoke_checks`
  恒返回元组，坏会话降级为 failed 项而非 INTERNAL 崩溃。
- e2e/inspector 测试同步当前工具面（移除对已删 `ppsspp_smoke_test` /
  `ppsspp_session_list` 的调用；期望集合改为从基线 JSON 派生，杜绝再次
  漂移）。

## [Unreleased]

## [0.1.5] - 2026-09-18

### Added

- **Python 3.13 支持**：全仓 17 处 PEP 758 无括号多异常捕获
  （`except A, B:` → `except (A, B):`，零行为差异）替换为括号形式，
  `requires-python` 放宽为 `>=3.13`，CI 测试矩阵扩展为
  3.13 + 3.14 × 三平台。mcp SDK 官方支持 3.10+，放宽采用门槛。

## [0.1.4] - 2026-09-18

### Added

- 英文 README（`README.md` 转为英文主门面，中文迁至 `README.zh-CN.md`，
  双语切换器）——面向 MCP 全球受众。
- MCP Registry 元数据：`server.json`（官方 Registry 发布格式）与 README
  的 `mcp-name` 所有权标记。

## [0.1.3] - 2026-09-18

### Changed

- README 增加徽章行（PyPI / CI / Python / License）。
- 全库 ruff 清债至零并纳入 CI 执法：safe-fix 482 处（导入排序/类型现代化/
  未用导入）、`ruff format` 全库 214 文件、残量 SIM105/SIM117/F841 等手工
  清扫；`.github/workflows/ci.yml` 新增 lint job（`ruff check` +
  `ruff format --check`）。
- 工具描述空白规范化（协议面唯一变化）：`ruff format` 剥离 docstring
  空行尾随空白，仅 `ppsspp_assemble` 描述受影响（490→478 字符，纯空白级），
  基线经 `scripts/dump_tool_surface.py` 同步再生成；41 工具 inputSchema/
  outputSchema 逐字节不变。
- 错误码分类学补全：输入参数校验统一为 `ARGS_INVALID`（全仓 83 处从
  `INTERNAL` 迁移；`.ppr` 写盘失败等真实内部错误保留 `INTERNAL`）。
  技能文档 `error-codes.md` 同步新增 `ARGS_INVALID`/`STEP_INVALID` 条目，
  `INTERNAL` 行改写为仅限服务器自身故障。
- `memory_protection` 的 top.prx 受保护段基址改从 `addresses.yaml` 的
  `top_base.ppsspp` 读取（缺失时回退本项目默认值），落实"无硬编码项目
  地址"原则；内核段保持 PSP 通用常量。
- `MAX_WAIT_FRAMES` / `MAX_PRESS_DURATION_FRAMES` 下移至
  `core/primitives.py`（单一真相源），`models/batch_step.py` 的字段描述
  改为插值引用，不再手抄数值。
- `ppsspp_batch_step` 的 `PressStep.button` 在 inputSchema 中以 25 项枚举
  下发（此前为裸 string + 文字描述，白名单约束仅存在于运行时）；按钮词汇表
  规范定义上收至 `models/input.py`（`PPSSPP_ALL_BUTTONS`），input 工具与
  批量步骤共用单一真相源。

### Fixed

- `batch_step` 的 wait step 补上帧上限执行（此前仅独立 `wait_frames`
  工具执行该 cap，批量路径的文档承诺未兑现）。
- `_validate_step` 的 step 结构校验错误码由 `INTERNAL` 更正为
  `STEP_INVALID`（输入校验失败不是服务器内部错误；新错误类继承
  `ToolError`，既有客户端分类不受影响）。

## [0.1.2] - 2026-09-17

### Fixed

- MCP 握手的 serverInfo 版本改为从包元数据读取（`importlib.metadata`），
  消除 `__init__.py` 中与 pyproject 脱节的硬编码副本——0.1.1 曾在协议层
  自报 0.1.0。
- README：Windows 的 venv 创建命令更正为 `py -3.14`
  （`python3.14` 别名在 Windows 上通常不存在）。

## [0.1.1] - 2026-09-17

### Added

- **配置模板三件套**（`examples/`）：`project.yaml` / `addresses.yaml` /
  `scripts.manifest.yaml` 可拷贝模板，PLACEHOLDER 标注 + 逐字段语义注释，
  独立部署时 `mkdir -p .ppsspp-dfx/config && cp examples/*.yaml
  .ppsspp-dfx/config/` 即可起步。
- **预置 `.mcp.json`**：独立 checkout 的开箱即用 MCP 客户端注册。
- `check_env.py --check` 对 `scripts.manifest.yaml` 缺失显式告警（非阻断）
  并给出 examples 修复路径——此前缺失会导致 `ppsspp_script_*` 工具静默消失。
- **README 新增「故障排查速查表」与「已知限制」**：错误码体系的症状级
  索引 + 协议面边界的诚实汇总。
- **`docs/SCOPE.md`**：PPSSPP WS 事件 → 工具映射的人读版 + 刻意未工具化
  事件清单（机器可读真相源仍为 `ws_contract.py`）。
- 随包 `.github/workflows/`：三平台测试矩阵（CI）与两段式发布工作流——
  push tag `v*` 构建并试发布（内部验证），确认后发布 GitHub Release 正式
  上架 PyPI；两阶段均为 trusted publishing（OIDC），内置 tag/版本一致性 Guard。

### Changed

- README 定位为中文社区发布；代码注释与工具 docstring 保持英文
  （协议面被工具面基线锁定）。
- README 参考 deepseek-harness 的官方 README 结构重构为发布版形态：
  PyPI 安装为主路径（含安装后的 `.mcp.json` 与命令示例），新增项目状态、
  致谢与引用节。

## [0.1.0] - 2026-09-16

### Added

- 首个开源发布版本：41 个静态 MCP 工具（全结构化 inputSchema/outputSchema）、
  动态诊断脚本工具（manifest 驱动）、多会话管理与楔死自愈、后台批处理、
  原生 replay 录制回放、GPU 缓冲/纹素/CLUT 转储、HLE 内省、盲测评估体系
  （21 场景卡 + 确定性门禁）。
