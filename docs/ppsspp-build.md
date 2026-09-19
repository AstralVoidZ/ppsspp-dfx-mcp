# PPSSPP 获取与 WebSocket 调试器开启指南

ppsspp-dfx-mcp 通过 WebSocket 与 PPSSPP 的调试器通信（子协议
`debugger.ppsspp.org`）。**不需要自行编译 PPSSPP**——官方发行版已内置该调试器，
开启一个设置即可。本文说明如何准备。

> 考证依据：本仓库镜像的 PPSSPP 源码中，WebSocket 调试器实现位于
> `Core/Debugger/WebSocket.cpp`，作为内置 web 调试服务器
> （`Core/WebServer.cpp`，`StartWebServer(WebServerFlags::DEBUGGER)`）的
> websocket upgrade 端点存在；`v1.15.0` 标签中该文件已存在。

## 1. 获取 PPSSPP

直接下载**官方发行版**（Windows 安装版/绿色版、Android APK、macOS、Linux 均可）：
<https://www.ppsspp.org/download/>。建议使用最新稳定版
（WebSocket 调试器在 v1.15.0 已内置，更早版本未验证）。

## 2. 开启 WebSocket 调试器

### 方式 A：设置界面（推荐，GUI 用户）

`Settings → Tools → Developer tools → Enable remote debugger`（打开开关）。

打开后 PPSSPP 立即启动调试 web 服务器（对应 `ppsspp.ini` 的
`RemoteDebuggerOnStartup = True`）；如需仅本机访问，可同时打开
`RemoteDebuggerLocal = True`。

### 方式 B：ppsspp.ini（可复用、可版本化）

把项目模板 [`skills/ppsspp-dfx/assets/ppsspp.ini.template`](../skills/ppsspp-dfx/assets/ppsspp.ini.template)
中的相关段落合并到你的 `ppsspp.ini`（Windows 绿色版放在 PPSSPP 目录下；
portable 模式放在 `memstick/PSP/SYSTEM/ppsspp.ini`）：

```ini
[General]
RemoteISOPort = 12345        # 调试器端口（与 MCP 侧默认端口一致）
RemoteDebuggerOnStartup = True
RemoteDebuggerLocal = True   # 仅监听本机
```

### 方式 C：Headless 命令行（CI / 自动化）

PPSSPPHeadless.exe 支持 `--debugger=<port>` 启动调试服务器（本项目 CI 的
real-wire 验证即走此路径）。

## 3. 与 MCP 侧对齐

- PPSSPP 调试器端口（ini 的 `RemoteISOPort`）与 MCP 侧
  `PPSSPP_DFX_WS_PORT`（默认 **12345**）保持一致即可，两侧默认值天然对齐，
  通常无需配置。
- 端口冲突时二选一修改：PPSSPP ini 端口，或 MCP 侧环境变量 /
  `.ppsspp-dfx/config/project.yaml`。

## 4. 连不上？

按 README 的[故障排查速查表](../README.md#故障排查速查表)处理，高频三项：

1. 设置开关没开（或开启后重启过 PPSSPP 但 ini 未保存）；
2. 端口不一致（PPSSPP ini vs `PPSSPP_DFX_WS_PORT`）；
3. PPSSPP UI 被模态对话框/暂停挡住（`WS_TIMEOUT` 的典型成因）。

## 5. 行为契约的验证基线

本项目的行为契约在以下构建上做过真机验证：

- **PPSSPP v1.20.4-605-gf0c28c6744**（Windows x64 官方发行版）

覆盖的行为契约包括：内存断点删除的严格报错语义（`mem_remove` 对不存在的
监控点报错而非静默成功）、`mem_update` 对省略布尔的合并语义（依赖 PPSSPP
对未传字段的零省略行为）、断点命中经 `cpu.stepping` 广播的观察通道、以及
README「性能参考」一节的全部数字。

`v1.15.0` 起调试器已内置，但其余版本的行为**未逐一验证**——升级 PPSSPP 后
若上述契约表现不一致，请提 issue 附上版本号，而不是默认契约失效。
