---
title: 截图与画面捕获
type: playbook
scope: common
category: screenshot
---

# Playbook: 截图与画面捕获

> 通用 PPSSPP 项目截图 playbook。字库纹理特化场景（纹理布局、字形覆盖校验）见项目专属 playbook。

## 何时使用

- 断点命中时记录当前画面（验证游戏状态）
- 视觉对比回归测试（原版 vs 修改版）
- 崩溃/冻结现场留证
- 对话推进时批量截图

## 判定信号

截图返回**两个通道**：`content` 含一个 `ImageContent`（像素），`structuredContent` 含元数据 `mode` / `source` / `file_path` / `width` / `height` / `format` / `size_bytes` / `empty`。**元数据不再以文本块返回**——它只在 `structuredContent` 里，`image_base64` 不在结构化通道中（像素走 `ImageContent`）：

| 信号 | 含义 | 路由 |
|------|------|------|
| `source=render` | renderColor 路径（默认，最高质量） | 直接用于视觉对比 |
| `source=render→vram_fallback` | render 空帧，已自动回退 VRAM 直读 | 仅结构参考（布局/黑屏判断），颜色不可靠 |
| `empty=true` | 空捕获 | 增加等待帧数；确认进入有渲染的场景 |
| `width×height ≈ 480×272` 倍数 | PSP 原生分辨率 | 正常 |
| 批量截图尺寸不一致 | 窗口被调整 | 启动会话前固定窗口尺寸 |

VRAM 回退与 GPU 渲染不同步：适合判断"画面是否黑屏/有无内容"，不适合颜色对比。

## 调用顺序

1. **进入目标场景** — 会话启动见 [smoke_test.md](smoke_test.md)；推进场景用 `ppsspp_press_button` + `ppsspp_wait_frames`（或 `ppsspp_batch_step` 编排）；断点命中后画面即暂停态，可直接截。
2. **截图** — `ppsspp_screenshot(session_id)`（默认 `source="render"`）。**不要传 `source="output"`**（CRASH-RISK）与弃用的 `mode` 参数（constraints B5）。
3. **校验** — 检查 `structuredContent`：`empty=true` → 加大 `wait_frames` 重试；`render→vram_fallback` → 仅作结构参考。`file_path` 指向工具自动落盘的 PNG/JPG，可直接复用而不必重新截图。
4. **纹理/CLUT 抓取**（字库/贴图分析）— `ppsspp_dump(kind="texture"/"clut")`：只抓"当前绑定"对象，不支持按 VRAM 地址；须在目标纹理正被使用的画面调用；空捕获报 `CAPTURE_EMPTY`。
5. **GPU 命令流**（渲染管线深查）— `ppsspp_gpu_record`（要求 CPU running）落盘 `.ppsspp-dfx/output/gpu_dumps/*.dump`。
6. **GPU 状态** — `ppsspp_gpu_stats` 返回 fps/vblanks/绘制统计；也可作 CPU 状态探针（见 [../../cpu-state-contract.md](../../cpu-state-contract.md) §3.2）。

## 错误处理

| 症状 | 原因 | 修复 |
|------|------|------|
| `empty=true` 反复出现 | 标题/加载屏无 renderColor 输出 | 属预期；进场景后重试，或接受 `render→vram_fallback` 的结构参考 |
| 截图间歇性失败 | VBlank 干扰 | 增加 `wait_frames` 后重试 |
| `CAPTURE_EMPTY`（texture/clut） | 目标纹理未被绑定 | 先推进到使用该纹理的画面 |
| `CPU_STATE_ERROR`（gpu_record/gpu_stats） | CPU 处于暂停态 | `step(action="resume")` 后再调 |
| PPSSPP 崩溃 | 误用 `source=output` | 移除该参数走默认 render 路径 |

## 关联资源

- 协议约束: [../../ppsspp-constraints.md](../../ppsspp-constraints.md)（B5 截图策略 / C1 帧数单位）
- 工具语义: [../../tool-surface.md](../../tool-surface.md)（截图与 GPU 族）
- 批量编排: [batch_automation.md](batch_automation.md)
