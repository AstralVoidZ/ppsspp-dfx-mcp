# 更新日志

本项目的所有显著变更将记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
