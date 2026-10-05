#!/usr/bin/env python3
"""check_env.py — ppsspp-dfx MCP server 的环境自检与自愈。

**为什么需要独立 venv**：server 代码于 `122e864` 迁到 MCP SDK v2
（`mcp.server.mcpserver.MCPServer`，1.x 无此模块），import 阶段即崩。本项目声明
`mcp[cli]>=2.1.1,<3`，而系统 Python 上的 `mcp` 常被其他 MCP server（ida-pro-mcp /
fastmcp / mcp-server-fetch）钉在 1.x——版本冲突不可调和。`.venv/` 被 `.gitignore`
排除，**新 clone 必然没有**，故需本工具自检与重建。

**`.mcp.json` 的启动形式（2026-10-03 变更，specs/010 T025/T026）**：现为**通用运行器**
形式 `uv run ppsspp-dfx-mcp`，不再指向平台绑定的 venv 解释器路径。变更理由：旧形式含
操作系统绑定的路径布局（Windows `Scripts/python.exe` vs POSIX `bin/python`），协作者
克隆即坏——specs/010 把它登记为遗留项 L4。

变更前的设计及其理由（保留作历史，以免后来者重蹈）：
  MCP 客户端在 stdio 传输下只看得见子进程「活着 / 退出」，包装层报的错传不出来；
  Windows 上 `os.execv` 实为 `CreateProcess` + 父进程等待（**不是** POSIX 的替换进程），
  多层嵌套后最内层 server 的 stdin 在 Node/libuv 管道下读不到数据，会静默 EOF 退出
  ——表现为无信息的 `-32000: Connection closed`。

**该顾虑在 `uv run` 形态下的实测结论（2026-10-03，Windows）**：**未复现**。完整握手成功：
`initialize` 返回 `serverInfo{name: ppsspp-dfx-mcp, version: 0.1.7}`，`tools/list` 返回
**37** 个工具。故单层 `uv run` 包装可用。**该验证尚未覆盖 POSIX**——若在 POSIX 上复现
静默 EOF，需回到"直接 spawn 解释器"形式并在两种平台各留一份配置。

Usage::

    python scripts/check_env.py              # 自检（默认）
    python scripts/check_env.py --check      # 同上，显式
    python scripts/check_env.py --bootstrap  # 创建/修复 venv（含各 .mcp.json 旁的 venv）
    python scripts/check_env.py --print-config  # 打印绝对路径的 mcpServers 片段

诊断输出一律走 stderr；`--print-config` 的 JSON 走 stdout（可管道）。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from _wire import PACKAGE_ROOT, WORKSPACE_ROOT

PROJECT_DIR = PACKAGE_ROOT
VENV_DIR = WORKSPACE_ROOT / ".venv" / "ppsspp-dfx-mcp"
VENV_DIR_REL = ".venv/ppsspp-dfx-mcp"
#: 提交的 `.mcp.json` MUST 使用的通用运行器形式（specs/010 FR-008 / FR-009）。
GENERAL_RUNNER = "uv"
RUNNER_ARGS = ("run", "ppsspp-dfx-mcp")
#: 平台绑定的解释器路径形态——提交的配置 MUST NOT 使用（两平台 venv 布局不同）。
_PLATFORM_BOUND_RE = re.compile(
    r"(?:^|[\\/])(?:Scripts[\\/]python\.exe|bin[\\/]python)$", re.IGNORECASE
)
#: 包入口名（`[project.scripts]` 声明）。
PACKAGE_ENTRY = "ppsspp-dfx-mcp"


def _runner_args_error(config_dir: Path, args: Any) -> str | None:
    """校验 `args`：接受两种**自足**形式，其余一律拒绝。

    形式 1（配置文件位于包根时）：`["run", PACKAGE_ENTRY]` —— 由运行器按 cwd 找到项目。
    形式 2（配置文件不在包根时）：`["run", "--directory", <包目录>, PACKAGE_ENTRY]` ——
              **必须**显式钉住包目录。仓库根那份配置属此类：从仓库根执行形式 1 会落到
              另一个项目（仓库根也有 pyproject.toml），`project_root()` 会解析到错误位置。

    FR-010 要求校验"执行目标存在"：故形式 2 的目录 MUST 存在且**看起来是包根**。
    """
    got = list(args or [])
    if got == list(RUNNER_ARGS):
        return None
    if len(got) == 4 and got[0] == "run" and got[1] == "--directory" and got[3] == PACKAGE_ENTRY:
        raw = got[2]
        target = Path(raw)
        resolved = target if target.is_absolute() else (config_dir / target)
        if not resolved.is_dir():
            return (
                f"--directory 目标不是目录：{raw!r}（按配置目录解析为 {resolved}）\n"
                "        FR-010：配置 MUST 指向存在的执行目标"
            )
        if not (resolved / "pyproject.toml").is_file():
            return f"--directory 目标不像包根（缺 pyproject.toml）：{resolved}"
        return None
    return (
        f"args 不合法：{got!r}\n"
        f"        合法形式：{list(RUNNER_ARGS)!r}（配置位于包根时）\n"
        f"        或：['run', '--directory', '<包目录>', {PACKAGE_ENTRY!r}]（配置不在包根时）"
    )


VENV_PYTHON_REL = ("Scripts/python.exe", "bin/python")  # Windows / POSIX
DOCTOR_REL = "scripts/check_env.py"
MIN_SDK = (2, 1, 1)

_SDK_PROBE = "import importlib.metadata as m; print(m.version('mcp'))"


def _configure_stderr() -> None:
    """强制 stderr 用 UTF-8。

    Windows 上 stderr 默认按 locale(cp936) 编码：非 GBK 字符（`✓` / `≥`）会退化成
    `\\u2713` 字面量，中文则与 UTF-8 终端（Git Bash / VS Code / Claude Code）对不上。
    """
    with contextlib.suppress(AttributeError, OSError, ValueError):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def _say(*parts: object) -> None:
    print(*parts, file=sys.stderr, flush=True)


def _display(path: Path) -> str:
    """仓库内路径显示为相对形式，便于复制粘贴。"""
    try:
        return path.relative_to(WORKSPACE_ROOT).as_posix()
    except ValueError:
        return str(path)


def runner_available() -> str | None:
    """通用运行器在本机的可执行路径（PATH 解析）；缺失返回 None。

    提交的 `.mcp.json` 依赖它，故自检 MUST 把它当作**一等前置**检查：运行器不在 PATH
    上时，配置在客户端里必然失败，而 stdio 传输下客户端只看得见「退出」，看不到原因。
    """
    return shutil.which(GENERAL_RUNNER)


def _venv_python_in(venv_dir: Path) -> Path | None:
    for rel in VENV_PYTHON_REL:
        candidate = venv_dir / rel
        if candidate.exists():
            return candidate
    return None


def _venv_python() -> Path | None:
    return _venv_python_in(VENV_DIR)


def _run(argv: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """跑子进程并捕获输出。

    不指定 `encoding`：子进程按 locale(cp936) 输出，须以同一编码解码；父进程随后
    以 UTF-8 重新写出（见 `_configure_stderr`）。`errors="replace"` 保证畸形字节
    不会变成异常。
    """
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=900
    )


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
    return (
        f"  修复：python {DOCTOR_REL} --bootstrap\n"
        f"        （创建 {VENV_DIR_REL} 并安装 ppsspp-dfx-mcp[dev]）"
    )


def _fail(headline: str, detail: str = "") -> int:
    _say(f"\n✘ {headline}")
    if detail:
        _say(f"  {detail}")
    _say(_remediation())
    return 1


def validate_mcp_entry(config_dir: Path, cfg: Any) -> str | None:
    """校验 config_dir 的 `.mcp.json` 是否符合**通用运行器**契约。

    纯函数：不读盘、不改写（唯一的例外是 `runner_available()`，它只查 PATH）。返回可
    执行的中文错误串，合规返回 None。

    契约（specs/010 FR-008 / FR-009 / contracts/local-launch.md C3-1…C3-4）：
      1. `command` MUST 是通用运行器名，MUST NOT 是平台绑定的解释器路径；
      2. `command` MUST 能在本机 PATH 上解析（否则客户端必然失败）；
      3. `args` MUST 恰为 `["run", "ppsspp-dfx-mcp"]`（自定位的包入口调用）；
      4. MUST NOT 默认注入调试级日志。

    为什么不再要求「本目录旁必须存在 venv」：运行器形式由 `uv` 自行管理环境，首次运行
    即创建，故 per-config venv 不再是前提（该供给已随旧契约退役）。
    """
    if not isinstance(cfg, dict):
        return ".mcp.json 顶层应为 JSON 对象"
    entry = (cfg.get("mcpServers") or {}).get("ppsspp-dfx") or {}
    if not entry:
        return ".mcp.json 中缺少 mcpServers.ppsspp-dfx"

    # 客户端把 ${...} 当环境变量展开，不是 VS Code 的 workspaceFolder 语义
    if "workspaceFolder" in json.dumps(cfg):
        return "配置含 ${workspaceFolder}：客户端按环境变量展开，必然失败"

    command = entry.get("command") or ""
    if not command:
        return "mcpServers.ppsspp-dfx.command 为空"
    if _PLATFORM_BOUND_RE.search(command):
        return (
            f"command 是平台绑定的解释器路径：{command!r}\n"
            "        该形态在另一操作系统上必然失效（Windows Scripts/ vs POSIX bin/）\n"
            f"        修复：改用通用运行器形式 command={GENERAL_RUNNER!r}, "
            f"args={list(RUNNER_ARGS)!r}"
        )
    if command != GENERAL_RUNNER:
        return (
            f"command 应为通用运行器 {GENERAL_RUNNER!r}，实际 {command!r}\n"
            f"        修复：改回 command={GENERAL_RUNNER!r}, args={list(RUNNER_ARGS)!r}"
        )
    if runner_available() is None:
        return (
            f"本机 PATH 上找不到运行器 {GENERAL_RUNNER!r}：该配置在客户端里必然失败\n"
            "        （stdio 传输下客户端只看得见子进程退出，看不到具体原因）\n"
            f"        修复：安装 {GENERAL_RUNNER} 并确保其在 PATH 上"
        )

    args = entry.get("args") or []
    args_error = _runner_args_error(config_dir, args)
    if args_error is not None:
        return args_error

    env = entry.get("env") or {}
    if isinstance(env, dict) and str(env.get("PPSSPP_DFX_LOG_LEVEL", "")).upper() == "DEBUG":
        return (
            "配置默认注入调试级日志（PPSSPP_DFX_LOG_LEVEL=DEBUG）\n"
            "        调试默认值会放大每次启动的日志与落盘面；需要时由使用者显式开启"
        )
    return None


def validate_mcp_config(config_dir: Path) -> str | None:
    """对 config_dir/.mcp.json 做「存在 + 可解析 + 入口合规」全套校验。"""
    cfg_path = config_dir / ".mcp.json"
    if not cfg_path.exists():
        return ".mcp.json 不存在"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f".mcp.json 无法解析：{exc}"
    return validate_mcp_entry(config_dir, cfg)


def mcp_config_dirs() -> list[Path]:
    """本检出必须保持可用的 `.mcp.json` 所在目录（按解析路径去重）。

    至少覆盖工作区根；monorepo 下包目录还带一份独立 `.mcp.json`，其相对
    command 同样必须在包目录旁找到 venv，故一并纳入；同一文件只列一次。
    """
    dirs: list[Path] = []
    seen: set[Path] = set()
    for cand in (WORKSPACE_ROOT, PACKAGE_ROOT):
        cfg_path = cand / ".mcp.json"
        if not cfg_path.is_file():
            continue
        key = cfg_path.resolve()
        if key in seen:
            continue
        seen.add(key)
        dirs.append(cand)
    return dirs


def ensure_venv(target_dir: Path) -> int | None:
    """在 target_dir 创建（或补装）venv。成功返回 None，失败返回退出码。

    复用原有 venv + pip/uv 安装逻辑与消息；抽成可 mock 的独立函数，以便
    bootstrap 为多个 `.mcp.json` 旁的 venv 复用，并便于结构测试。
    """
    _say(f"[bootstrap] 目标 venv：{_display(target_dir)}")
    python = _venv_python_in(target_dir)
    if python is None:
        _say(f"[bootstrap] {sys.executable} -m venv")
        proc = _run([sys.executable, "-m", "venv", str(target_dir)])
        if proc.returncode != 0:
            _say(proc.stdout)
            _say(proc.stderr)
            return _fail("创建 venv 失败")
        python = _venv_python_in(target_dir)
        if python is None:
            return _fail("venv 创建后仍找不到解释器", f"预期路径：{target_dir}")

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
    return None


def bootstrap() -> int:
    """创建（或补装）主 venv，并为每个提交的 `.mcp.json` 补齐其旁的 venv。

    提交的 `.mcp.json` 只能指向它自己目录旁的 venv（相对 command 由 config
    目录解析，且策略禁止提交绝对路径）——因此每个候选目录的 venv 都必须存在，
    该配置才可能被客户端解析成功。
    """
    rc = ensure_venv(VENV_DIR)
    if rc is not None:
        return rc

    # 运行器形式（specs/010 T025）下不再需要为每份配置旁补 venv：`uv` 首次运行即自建
    # 环境。原先的 per-config venv 供给随旧契约一并退役；提交的配置只剩「运行器是否
    # 可用」这一个环境前提，由 check() 检查。
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
        ver = _run([str(python), "-c", "import sys; print(sys.version.split()[0])"]).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        ver = "?"
    _say(f"解释器      {_display(python)}  (Python {ver})  ✓")

    sdk = _sdk_version(python)
    if sdk is None:
        _say("mcp SDK     ✘ 未安装或解释器不可用")
        _say("─" * 60)
        return _fail("venv 内缺少 mcp 包")
    if _version_tuple(sdk) < MIN_SDK:
        _say(f"mcp SDK     {sdk}  ✘ 版本过低（要求 ≥{'.'.join(map(str, MIN_SDK))},<3）")
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
        _say(
            "            → 所有 ppsspp_script_* 工具将不可用；"
            "从 examples/ 拷贝三份 yaml 模板到 .ppsspp-dfx/config/ 即可修复"
        )

    # 通用运行器：提交的 .mcp.json 依赖它（specs/010 FR-008 / FR-010）
    runner = runner_available()
    if runner is None:
        _say(f"运行器      {GENERAL_RUNNER}  ✘ 不在 PATH 上")
        _say("─" * 60)
        return _fail(f"提交的 .mcp.json 使用通用运行器 {GENERAL_RUNNER!r}，但本机解析不到它")
    _say(f"运行器      {GENERAL_RUNNER}  ✓  {_display(Path(runner))}")

    # MCP 配置：逐个校验本检出必须保持可用的 .mcp.json（工作区根 + 包目录）
    failed: list[str] = []
    for config_dir in mcp_config_dirs():
        label = _display(config_dir / ".mcp.json")
        issue = validate_mcp_config(config_dir)
        if issue:
            _say(f"MCP 配置    {label}  ✘ {issue}")
            failed.append(label)
        else:
            _say(f"MCP 配置    {label}  ✓")
    if failed:
        _say("─" * 60)
        _say(f"\n✘ 有 {len(failed)} 份 MCP 配置与 venv 不一致（不会自动改写，请手工修正）")
        for label in failed:
            _say(f"  - {label}")
        _say(f"  修复：python {DOCTOR_REL} --print-config（给出钉住包目录的绝对路径变体）")
        _say("        或按上述契约纠正 .mcp.json（通用运行器形式，勿写平台绑定路径）")
        return 1

    _say("─" * 60)
    _say("结论：环境就绪")
    return 0


def print_config() -> int:
    """输出可直接粘贴的 mcpServers 片段（stdout，合法 JSON）。

    与仓库内提交的相对配置**同形**（通用运行器），但用 `--directory` 钉住包目录的
    **绝对路径**，面向「不把 command 解析到配置所在目录」的客户端——这类客户端下相对
    定位会落到错误的项目：`project_root()` 的规则是 env > cwd，cwd 一旦是别处，输出
    目录与脚本清单都会错位。
    """
    if runner_available() is None:
        return _fail(f"本机 PATH 上找不到运行器：{GENERAL_RUNNER}")
    snippet = {
        "mcpServers": {
            "ppsspp-dfx": {
                "command": GENERAL_RUNNER,
                "args": ["run", "--directory", str(PROJECT_DIR), *RUNNER_ARGS[1:]],
            }
        }
    }
    print(json.dumps(snippet, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    _configure_stderr()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="环境自检（默认行为）")
    parser.add_argument("--bootstrap", action="store_true", help="创建/修复 venv 并安装子项目依赖")
    parser.add_argument(
        "--print-config", action="store_true", help="打印绝对路径的 mcpServers 片段（stdout）"
    )
    args = parser.parse_args()

    if args.bootstrap:
        return bootstrap()
    if args.print_config:
        return print_config()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
