#!/usr/bin/env python3
"""check_env.py — ppsspp-dfx MCP server 的环境自检与自愈。

**为什么需要独立 venv**：server 代码于 `122e864` 迁到 MCP SDK v2
（`mcp.server.mcpserver.MCPServer`，1.x 无此模块），import 阶段即崩。本项目声明
`mcp[cli]>=2.1.1,<3`，而系统 Python 上的 `mcp` 常被其他 MCP server（ida-pro-mcp /
fastmcp / mcp-server-fetch）钉在 1.x——版本冲突不可调和。`.venv/` 被 `.gitignore`
排除，**新 clone 必然没有**，故需本工具自检与重建。

**为什么 `.mcp.json` 直接指向 venv 解释器、而不是经过包装脚本**：MCP 客户端在
stdio 传输下只看得见子进程「活着 / 退出」，包装层报的错传不出来；更要命的是
Windows 上 `os.execv` 实为 `CreateProcess` + 父进程等待（**不是** POSIX 的替换
进程），多层嵌套后最内层 server 的 stdin 在 Claude Code(Node/libuv) 创建的管道下
读不到数据，会静默 EOF 退出——表现为无信息的 `-32000: Connection closed`。
因此配置层只做最直接的一件事：spawn venv 解释器。

Usage::

    python scripts/check_env.py            # 自检（默认）
    python scripts/check_env.py --check    # 同上，显式
    python scripts/check_env.py --bootstrap  # 创建/修复 venv

诊断输出一律走 stderr。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from _wire import PACKAGE_ROOT, WORKSPACE_ROOT

PROJECT_DIR = PACKAGE_ROOT
VENV_DIR = WORKSPACE_ROOT / ".venv" / "ppsspp-dfx-mcp"
VENV_DIR_REL = ".venv/ppsspp-dfx-mcp"
VENV_PYTHON_REL = ("Scripts/python.exe", "bin/python")  # Windows / POSIX
DOCTOR_REL = "mcps/ppsspp-dfx-mcp/scripts/check_env.py"
MIN_SDK = (2, 1, 1)

_SDK_PROBE = "import importlib.metadata as m; print(m.version('mcp'))"


def _configure_stderr() -> None:
    """强制 stderr 用 UTF-8。

    Windows 上 stderr 默认按 locale(cp936) 编码：非 GBK 字符（`✓` / `≥`）会退化成
    `\\u2713` 字面量，中文则与 UTF-8 终端（Git Bash / VS Code / Claude Code）对不上。
    """
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError, ValueError):
        pass


def _say(*parts: object) -> None:
    print(*parts, file=sys.stderr, flush=True)


def _display(path: Path) -> str:
    """仓库内路径显示为相对形式，便于复制粘贴。"""
    try:
        return path.relative_to(WORKSPACE_ROOT).as_posix()
    except ValueError:
        return str(path)


def _venv_python() -> Path | None:
    for rel in VENV_PYTHON_REL:
        candidate = VENV_DIR / rel
        if candidate.exists():
            return candidate
    return None


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """跑子进程并捕获输出。

    不指定 `encoding`：子进程按 locale(cp936) 输出，须以同一编码解码；父进程随后
    以 UTF-8 重新写出（见 `_configure_stderr`）。`errors="replace"` 保证畸形字节
    不会变成异常。
    """
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          errors="replace", timeout=900)


def _sdk_version(python: Path) -> str | None:
    try:
        proc = _run([str(python), "-c", _SDK_PROBE], cwd=PROJECT_DIR)
    except (OSError, subprocess.SubprocessError):
        return None
    version = proc.stdout.strip()
    return version if proc.returncode == 0 and version else None


def _version_tuple(text: str) -> tuple[int, ...]:
    """宽松解析 `2.2.0` / `2.1.1rc1` 这类版本串为可比较元组。"""
    out: list[int] = []
    for chunk in text.split(".")[:3]:
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _remediation() -> str:
    return (f"  修复：python {DOCTOR_REL} --bootstrap\n"
            f"        （创建 {VENV_DIR_REL} 并安装 mcps/ppsspp-dfx-mcp[dev]）")


def _fail(headline: str, detail: str = "") -> int:
    _say(f"\n✘ {headline}")
    if detail:
        _say(f"  {detail}")
    _say(_remediation())
    return 1


def _mcp_config_issue(vpython: Path) -> str | None:
    """校验 .mcp.json 仍指向 venv 解释器且不含展开不了的变量。"""
    cfg_path = WORKSPACE_ROOT / ".mcp.json"
    if not cfg_path.exists():
        return ".mcp.json 不存在"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f".mcp.json 无法解析：{exc}"

    entry = ((cfg.get("mcpServers") or {}).get("ppsspp-dfx") or {})
    if not entry:
        return ".mcp.json 中缺少 mcpServers.ppsspp-dfx"

    # Claude Code 的 ${...} 是环境变量展开，不是 VS Code 的 workspaceFolder
    if "workspaceFolder" in json.dumps(cfg):
        return "配置含 ${workspaceFolder}：Claude Code 按环境变量展开，必然失败"

    command = entry.get("command") or ""
    if not command:
        return "mcpServers.ppsspp-dfx.command 为空"
    resolved = Path(command) if os.path.isabs(command) else (WORKSPACE_ROOT / command)
    if resolved.resolve() != vpython.resolve():
        return (f"command 未指向 venv 解释器：{command!r}\n"
                f"        应为 {VENV_DIR_REL}/{VENV_PYTHON_REL[0]}")

    if (entry.get("args") or [])[:1] != ["-m"]:
        return f"args 应以 ['-m', 'ppsspp_dfx_mcp'] 开头：{entry.get('args')!r}"
    return None


def bootstrap() -> int:
    """创建（或补装）子项目 venv。优先用 uv 安装，回退到 pip。"""
    _say(f"[bootstrap] 目标 venv：{_display(VENV_DIR)}")
    python = _venv_python()
    if python is None:
        _say(f"[bootstrap] {sys.executable} -m venv")
        proc = _run([sys.executable, "-m", "venv", str(VENV_DIR)])
        if proc.returncode != 0:
            _say(proc.stdout)
            _say(proc.stderr)
            return _fail("创建 venv 失败")
        python = _venv_python()
        if python is None:
            return _fail("venv 创建后仍找不到解释器", f"预期路径：{VENV_DIR}")

    target = ".[dev]"  # dev 依赖为跑子项目测试所需，一次装好
    if shutil.which("uv"):
        argv = ["uv", "pip", "install", "--python", str(python), "-e", target]
    else:
        argv = [str(python), "-m", "pip", "install", "-e", target]
    _say(f"[bootstrap] {' '.join(argv[:4])} ... -e {target}")
    proc = _run(argv, cwd=PROJECT_DIR)
    if proc.returncode != 0:
        _say(proc.stdout)
        _say(proc.stderr)
        return _fail("安装子项目依赖失败")

    _say("[bootstrap] 完成，转入自检：\n")
    return check()


def check() -> int:
    """环境自检：解释器 / SDK 版本 / 包导入 / 项目配置 / MCP 配置一致性。"""
    _say("ppsspp-dfx MCP 环境自检")
    _say("─" * 60)
    _say(f"仓库根      {WORKSPACE_ROOT}")

    python = _venv_python()
    if python is None:
        _say(f"venv        {VENV_DIR_REL}  ✘ 不存在")
        _say("─" * 60)
        return _fail("独立 venv 缺失（.venv/ 被 .gitignore 排除，新 clone 必然没有）")

    _say(f"venv        {VENV_DIR_REL}  ✓")
    try:
        ver = _run([str(python), "-c",
                    "import sys; print(sys.version.split()[0])"]).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        ver = "?"
    _say(f"解释器      {_display(python)}  (Python {ver})  ✓")

    sdk = _sdk_version(python)
    if sdk is None:
        _say("mcp SDK     ✘ 未安装或解释器不可用")
        _say("─" * 60)
        return _fail("venv 内缺少 mcp 包")
    if _version_tuple(sdk) < MIN_SDK:
        _say(f"mcp SDK     {sdk}  ✘ 版本过低"
             f"（要求 ≥{'.'.join(map(str, MIN_SDK))},<3）")
        _say("─" * 60)
        return _fail("mcp SDK 版本不满足 server 的 SDK v2 导入路径")
    _say(f"mcp SDK     {sdk}  ✓ (要求 ≥{'.'.join(map(str, MIN_SDK))},<3)")

    proc = _run([str(python), "-c", "import ppsspp_dfx_mcp"], cwd=PROJECT_DIR)
    if proc.returncode != 0:
        _say("包导入      ppsspp_dfx_mcp  ✘")
        _say(proc.stderr.strip()[-800:])
        _say("─" * 60)
        return _fail("ppsspp_dfx_mcp 无法导入")
    _say("包导入      ppsspp_dfx_mcp  ✓")

    manifest = WORKSPACE_ROOT / ".ppsspp-dfx" / "config" / "scripts.manifest.yaml"
    if manifest.exists():
        _say(f"项目配置    {_display(manifest)}  ✓")
    else:
        # 非阻断：manifest 缺失时 server 仍可启动（警告 + 空清单），但所有
        # ppsspp_script_<name> 动态工具会静默消失——必须把修复路径说清楚。
        _say(f"项目配置    {_display(manifest)}  ✘ 缺失（警告，不阻断）")
        _say("            → 所有 ppsspp_script_* 工具将不可用；"
             "从 examples/ 拷贝三份 yaml 模板到 .ppsspp-dfx/config/ 即可修复")

    issue = _mcp_config_issue(python)
    if issue:
        _say(f"MCP 配置    .mcp.json  ✘ {issue}")
        _say("─" * 60)
        _say("\n✘ .mcp.json 与 venv 不一致（不会自动改写，请手工修正）")
        return 1
    _say("MCP 配置    .mcp.json 指向 venv 解释器  ✓")

    _say("─" * 60)
    _say("结论：环境就绪")
    return 0


def main() -> int:
    _configure_stderr()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="环境自检（默认行为）")
    parser.add_argument("--bootstrap", action="store_true",
                        help="创建/修复 venv 并安装子项目依赖")
    args = parser.parse_args()

    if args.bootstrap:
        return bootstrap()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
