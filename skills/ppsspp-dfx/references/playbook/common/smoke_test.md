---
title: ISO 启动 Smoke Test
type: playbook
scope: common
category: smoke-test
addresses: [game_mode]
---

# Playbook: ISO 启动 Smoke Test

> 通用 PPSSPP 项目启动验证 playbook，跨 PSP 项目可复用。项目特定验证项（模块加载、hook 注入）见项目专属 playbook。地址常量值由 `.ppsspp-dfx/config/addresses.yaml` 维护（`ppsspp_list_addresses` 可列出）。

## 何时使用

- 构建产物（cn.iso 等）后首次验证能否正常启动
- 修改 EBOOT.BIN / PRX 模块 / armips 补丁后回归测试
- PPSSPP 报"文件未找到"或启动即崩
- 怀疑 ISO 重建有问题（mkisofs 参数、文件名大小写）

## 判定信号

| 信号 | 含义 | 路由 |
|------|------|------|
| 四项检查全过 + `game_mode != 0` | 启动验证通过 | 进入下一步调试 |
| `iso_loaded=false` | ISO 未被识别 | 检查 ISO 完整性；确认 mkisofs 用了 `-iso-level 4 -xa`（见 constraints C2） |
| `ws_connected=false` | WS 调试器未连上 | 检查 `RemoteDebuggerOnStartup=True`；端口；防火墙（见 [../../../assets/ppsspp.ini.template](../../../assets/ppsspp.ini.template)） |
| `cpu_running=false` | CPU 未运行 | 确认 PPSSPP 带窗口启动；等待加载完成再测 |
| `game_mode=0` 持续 | 卡在标题/加载早期 | 增加等待；对照项目特定 smoke 项 |
| 日志含 "file does not exist" | ISO 重建问题（大小写） | mkisofs 必须带 `-iso-level 4 -xa` |
| 日志含 "Bad Execution Address" / "Bad memory access" | 运行时崩溃 | 转 [crash_analysis.md](crash_analysis.md) |

> PC 位置不作为 smoke 判定：VBlank 会误导 RUNNING 态 PC 采样（constraints A2）。判定只看四项检查 + `game_mode` + 日志。

## 调用顺序

1. **启动会话** — `ppsspp_health` → `ppsspp_session(action="start", iso_path=<绝对路径>)`，取回 `session_id`。会话启动自动完成：IR Interpreter 模式确认、WS `version` 握手。
2. **等待加载** — `ppsspp_wait_frames(frames=600~1800)`（大 ISO 首次加载更久），期间 PPSSPP 完成启动动画进入可交互态。
3. **健康检查** — `ppsspp_smoke_test(session_id)` 返回四项：
   - `iso_loaded` / `cpu_running` / `ws_connected` / `game_mode_valid`
   - `overall_status: fail` 时按判定信号表逐项路由
4. **游戏状态确认** — `ppsspp_read_memory(action="read_u32", address=<game_mode 地址>)`（地址 `ppsspp_list_addresses` 查询）；`0`=标题/加载，非零=已进游戏主循环。也可用预置探针：`ppsspp_state_observer(action="observe", names="game_mode")`。
5. **日志扫描** — `ppsspp_analyze_log()`（默认读广播日志镜像 `.ppsspp-dfx/output/ppsspp.log`），过滤 ERROR/WARNING/CRASH；可加 `filter` 关键词（如 "does not exist"、"Bad"）。
6. **FAIL 时留存现场** — `ppsspp_screenshot`（标题屏可能走 `render→vram_fallback`，正常现象）后转对应 playbook。

## 错误处理

| 症状 | 原因 | 修复 |
|------|------|------|
| `PORT_CONFLICT` | 端口被旧会话/进程占用 | `ppsspp_session(action="list")` 查旧会话并 stop；或确认无残留 PPSSPP 进程 |
| 反复 `WS_TIMEOUT` | PPSSPP 假启动（无窗口/被杀） | 检查进程与窗口；按 [cpu-state-contract.md](../../cpu-state-contract.md) §4 归因 |
| 标题→菜单转换期进程消失 | 项目已知不稳定段 | 查 `DUMP/log.txt` 与 `*.ppdmp` 转储；转 [crash_analysis.md](crash_analysis.md) |

## 关联资源

- 协议约束: [../../ppsspp-constraints.md](../../ppsspp-constraints.md)（C2 mkisofs / C3 IR Interpreter / B4 WS 握手）
- CPU 状态探针: [../../cpu-state-contract.md](../../cpu-state-contract.md)
- 崩溃跟进: [crash_analysis.md](crash_analysis.md)
- 截图降级: [screenshot.md](screenshot.md)
