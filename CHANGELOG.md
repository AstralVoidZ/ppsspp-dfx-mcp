# 更新日志

本项目的所有显著变更将记录在此文件。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [Unreleased]

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
- 随包 `.github/workflows/`：三平台测试矩阵（CI）与 tag 驱动 PyPI 发布
  （trusted publishing，需在 PyPI 侧配置 publisher 后生效）。

### Changed

- README 定位为中文社区发布；代码注释与工具 docstring 保持英文
  （协议面被工具面基线锁定）。

## [0.1.0] - 2026-09-16

### Added

- 首个开源发布版本：41 个静态 MCP 工具（全结构化 inputSchema/outputSchema）、
  动态诊断脚本工具（manifest 驱动）、多会话管理与楔死自愈、后台批处理、
  原生 replay 录制回放、GPU 缓冲/纹素/CLUT 转储、HLE 内省、盲测评估体系
  （21 场景卡 + 确定性门禁）。
