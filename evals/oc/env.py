"""MCP server 环境构造、XDG 隔离、会话预置。

本模块消除旧实现里三处**彼此重复且各自漂移**的 env 拼装逻辑
（`evals/runner.py:run_scenario`、`evals/bridge.py:_build_env`、
`evals/opencode_collect.py:_build_env`），把它们收敛为单一真源。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from evals.oc.errors import ErrorKind
from evals.oc.log import get_logger

_log = get_logger("evals.oc.env")

#: XDG 隔离根目录的环境变量（本机可指向别处，便于把采集态与日常 opencode 分开）
XDG_ROOT_ENV = "PPSSPP_DFX_EVALS_OC_XDG_ROOT"

#: 隔离时**重定向**的 XDG 变量：opencode 的数据树（session 历史、provider
#: 配置）与配置树。隔离这两棵就足以达成「采集不污染日常 opencode 状态」的目标。
_XDG_REDIRECTED = (
    ("XDG_DATA_HOME", "share"),
    ("XDG_CONFIG_HOME", "config"),
)

#: 隔离时**必须继承**的 XDG 变量。
#:
#: 2026-09-30 实测（`opencode mcp list` 对照实验）：把 `XDG_STATE_HOME` 一并
#: 重定向会让 CLI 报 `Error: Timed out waiting for the background service to
#: start`——opencode v2 有个常驻后台 service，其握手端点由 STATE 树定位；CLI 换到
#: 新 STATE 树后找不到已运行的 service，也不会（在该树内）拉起一个，于是 MCP
#: server 永远不加载，模型侧表现为 `Unknown tool 'ppsspp-dfx.ppsspp_xxx'`。
#: 三行实测对照：全隔离=service 超时；只隔离 DATA+CONFIG=0s 且
#: `ppsspp-dfx connected`；不隔离=同样 0s connected。故 STATE/CACHE 一律继承。
_XDG_INHERITED = (
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
)

#: 全部 XDG 变量 → 隔离目录下的子目录名（config/ 与 share/ 是两棵独立的树）
_XDG_DIR_NAME = dict(_XDG_REDIRECTED)

#: 隔离时需要从真实 profile 拷入的 opencode 子目录（XDG 变量名, profile 相对子目录）。
#: **这是旧实现的关键缺陷修复**：旧 `_xdg_env` 把四个 XDG 变量全部重定向，
#: 而配套的 `_write_provider_config` 是 `pass` 空实现——于是隔离环境里既没有
#: provider 配置也没有认证信息，opencode 必然无法完成调用。隔离的意图是
#: 「不让采集污染日常 opencode 状态」，不是「切断认证」，故这里改为
#: **拷贝**而非清空：读写隔离，认证可用。
_SEEDED_SUBDIRS = (
    ("XDG_CONFIG_HOME", "opencode"),
    ("XDG_DATA_HOME", "opencode"),
)

_WIN_CONFIG_ENVS = ("APPDATA",)
_WIN_DATA_ENVS = ("LOCALAPPDATA", "APPDATA")


_WIN_EXTRA_ROOTS = (
    ("APPDATA", "Roaming"),
    ("LOCALAPPDATA", "Local"),
)


def _profile_sources(var: str) -> list[Path]:
    """真实 profile 里该 XDG 变量的候选源目录（按优先级）。

    返回**列表**而非单值：opencode v2 在 Windows 上仍把状态库放在
    `~/.local/share/opencode/`（POSIX 风格路径），配置/插件则可能落在
    `%APPDATA%/opencode/`。只按平台假设单一位置会漏拷——漏拷的代价是隔离环境
    没有认证，表现为「模型不可用」而非任何显式错误。故此处穷举并集，由调用方
    对每个存在的源逐一拷贝。
    """
    candidates: list[Path] = []
    explicit = os.environ.get(var)
    if explicit:
        candidates.append(Path(explicit))
    if os.name == "nt":
        wanted = _WIN_CONFIG_ENVS if var == "XDG_CONFIG_HOME" else _WIN_DATA_ENVS
        for key in wanted:
            val = os.environ.get(key)
            if val:
                candidates.append(Path(val))
        # v2 状态库实测在 POSIX 风格路径，Windows 上同样使用
        candidates.append(Path.home() / ".local" / "share")
        candidates.append(Path.home() / ".config")
    else:
        candidates.append(Path.home() / (".config" if var == "XDG_CONFIG_HOME" else ".local/share"))
    seen: set[Path] = set()
    out: list[Path] = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def isolated_xdg_env(
    base_env: dict[str, str] | None = None,
    *,
    run_id: str = "",
) -> dict[str, str]:
    """为一次采集构造 XDG 隔离环境。

    隔离目录按 `run_id` 唯一——旧实现用固定的 `%TEMP%/oc-xdg`，两个并发 run
    会共享同一份 opencode 状态库（正是 `database is locked` 的来源）。

    只重定向 DATA/CONFIG，STATE/CACHE 继承（原因见 `_XDG_INHERITED`）。
    """
    env = dict(base_env if base_env is not None else os.environ)
    root = os.environ.get(XDG_ROOT_ENV) or str(Path(tempfile.gettempdir()) / "oc-evals-xdg")
    suffix = f"-{run_id}" if run_id else f"-{os.getpid()}"
    base = Path(root) / f"run{suffix}"
    for key, sub in _XDG_REDIRECTED:
        d = base / sub
        d.mkdir(parents=True, exist_ok=True)
        env[key] = str(d)
    _seed_opencode_profile(env, base)
    return env


#: 拷贝时跳过的子目录——opencode 内部的对象/快照库，纯缓存，且在 Windows 上
#: 常带只读属性（实测整棵 `snapshot/**/objects/**` 拷贝会 Permission denied）。
_SKIP_DIRS = frozenset({"snapshot", "log", "cache", "node_modules", ".git"})


def _tolerant_copy(src: str, dst: str) -> str | None:
    """单文件拷贝，失败则留痕并跳过（返回 dst 或 None）。

    `shutil.copytree` 默认遇错即抛，一颗只读文件就能让**整个** profile 拷贝失败，
    进而隔离环境没有认证——表现为「模型不可用」而非任何显式错误。逐文件容错把
    失败范围收敛到单个文件。
    """
    try:
        return shutil.copy2(src, dst)
    except OSError as exc:
        _log.debug("XDG profile 单文件拷贝跳过 %s: %s", src, exc)
        return None


def _skip_heavy(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in _SKIP_DIRS}


def _seed_opencode_profile(env: dict[str, str], iso_base: Path) -> None:
    """把真实 profile 里的 opencode 配置/数据拷入隔离目录。

    只拷 `opencode` 子目录而非整个 XDG 根——其他工具的缓存与本采集无关。
    源不存在时静默跳过（首次使用/未登录属正常），但会留痕：「拷不到」与
    「拷到了」在调用失败时的排查方向完全不同。
    """
    for key, tool_dir in _SEEDED_SUBDIRS:
        xdg_sub = _XDG_DIR_NAME[key]
        dst = iso_base / xdg_sub / tool_dir
        for root in _profile_sources(key):
            src = root / tool_dir
            if not src.is_dir():
                _log.debug("opencode profile 源不存在，跳过: %s", src)
                continue
            try:
                shutil.copytree(
                    src,
                    dst,
                    dirs_exist_ok=True,
                    ignore=_skip_heavy,
                    copy_function=_tolerant_copy,
                )
                _log.debug("XDG profile 已拷入: %s → %s", src, dst)
            except OSError as exc:
                # 认证缺失会在调用期表现为模型不可用，此处留痕便于二分定位
                _log.warning("XDG profile 拷贝失败 %s → %s: %s", src, dst, exc)


def build_server_env(
    *,
    real_mode: bool,
    fixtures_dir: Path,
    sessions_dir: Path,
    src_root: Path,
    tests_root: Path,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """构造 ppsspp-dfx-mcp server 子进程的环境（fake / real 共用单一真源）。

    fake 模式：`PPSSPP_DFX_TEST_MODE=fake` + `PPSSPP_DFX_FIXTURE_DIR`；
    real 模式：摘掉 fake 两项，透传 `PPSSPP_DFX_EXE_PATH`。

    `PPSSPP_DFX_SESSIONS_PATH` 由调用方给到每 run 独立的 tempfile——run 间
    状态不串。`PYTHONPATH` 注 src+tests：src 供包发现，tests 供 FakeTransport
    （`client_helper._build_fake_transport_for_session` 依赖 tests 树）。
    """
    env = dict(base_env if base_env is not None else os.environ)
    if real_mode:
        env.pop("PPSSPP_DFX_TEST_MODE", None)
        env.pop("PPSSPP_DFX_FIXTURE_DIR", None)
        env["PPSSPP_DFX_EXE_PATH"] = os.environ.get(
            "PPSSPP_DFX_TEST_EXE_PATH", "PPSSPPWindows64.exe"
        )
    else:
        env["PPSSPP_DFX_TEST_MODE"] = "fake"
        env["PPSSPP_DFX_FIXTURE_DIR"] = str(fixtures_dir)
    env["PPSSPP_DFX_LOG_LEVEL"] = "WARNING"
    env["PPSSPP_DFX_SESSIONS_PATH"] = str(sessions_dir / "sessions.json")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(src_root), str(tests_root), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return env


def resolve_iso(real_mode: bool) -> str:
    """场景 prompt 里 `{{REAL_ISO}}` 的替换值。"""
    return os.environ.get("PPSSPP_DFX_TEST_ISO_PATH", "game.iso") if real_mode else "fake_game.iso"


# ── 会话预置 ───────────────────────────────────────────────────────────


class _SeedResult:
    """预置结果：成功给 session id 列表，失败给原因——两者都必须落进轨迹。"""

    __slots__ = ("session_ids", "error")

    def __init__(self, session_ids: list[str], error: str | None) -> None:
        self.session_ids = session_ids
        self.error = error


def seed_sessions(
    count: int,
    iso_path: str,
    env: dict[str, str],
    *,
    real_mode: bool,
    settle_s: float = 0.0,
    call_timeout_s: float = 120.0,
) -> _SeedResult:
    """预置 `count` 个会话，写入本 run 的 sessions.json。

    ## 为什么可行

    opencode 通道的 MCP server 由 **opencode 拉起**，不是我们拉起——所以无法像
    `runner.py:_seed_sessions` 那样在同一条 ClientSession 上预置。这里改为：先用
    一条**短命**的 MCP 会话（自己的 server 子进程）预置并落盘，再退出；随后
    opencode 拉起的 server 读到同一份 `PPSSPP_DFX_SESSIONS_PATH`。

    恢复语义已核对 `session_manager._load_sessions`（`session_manager.py:101`）：
    载入时**不做 pid 存活校验**，并打 `extra["restored"]=True`。fake 模式下
    `client_helper.session_client_with_transport`（`client_helper.py:194`）按
    `session_id` **懒建** FakeTransport，故恢复出的会话完全可用。

    ## 限制

    - 仅 fake 模式可靠。real 模式下恢复出的会话其 PPSSPP 进程已随短命 server
      退出，`_session_transport_context` 会退化为每调用新建 WsTransport 而连不上
      已死的进程——因此 real 模式**默认不预置**，改由 agent 自行 `ppsspp_session
      start`（与旧行为一致）。
    - 失败不抛：预置失败记 `error` 进轨迹，门禁据实评分，调用方可见原因。
    """
    if count <= 0:
        return _SeedResult([], None)
    if real_mode:
        return _SeedResult([], "real 模式不预置：恢复出的会话其 PPSSPP 进程已随预置 server 退出")

    async def _do() -> list[str]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=sys.executable, args=["-m", "ppsspp_dfx_mcp"], env=env
        )
        ids: list[str] = []
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            for _ in range(count):
                result = await session.call_tool(
                    "ppsspp_session",
                    {
                        "action": "start",
                        "iso_path": iso_path,
                        "wait_ready": True,
                        "resilient": True,
                    },
                )
                if getattr(result, "is_error", getattr(result, "isError", False)):
                    raise RuntimeError(f"session start 失败: {_result_text(result)[:200]}")
                sc = (
                    getattr(result, "structured_content", None)
                    or getattr(result, "structuredContent", None)
                    or {}
                )
                sid = sc.get("session_id")
                if not sid:
                    try:
                        sid = json.loads(_result_text(result)).get("session_id")
                    except (ValueError, AttributeError):
                        sid = None
                if not sid:
                    raise RuntimeError(
                        f"session start 未返回 session_id: {_result_text(result)[:200]}"
                    )
                ids.append(str(sid))
        return ids

    try:
        ids = asyncio.run(asyncio.wait_for(_do(), timeout=call_timeout_s))
    except Exception as exc:  # noqa: BLE001 — 预置失败不阻断采集，记因进轨迹
        _log.warning("会话预置失败（count=%d）: %s: %s", count, type(exc).__name__, exc)
        return _SeedResult([], f"{type(exc).__name__}: {exc}"[:300])
    if settle_s > 0:
        time.sleep(settle_s)
    _log.debug("会话预置成功: %s", ids)
    return _SeedResult(ids, None)


def _result_text(result: Any) -> str:
    """工具结果拍平（与 `evals.runner._result_text` 同口径，SDK 版本大小写兼容）。"""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        elif getattr(block, "data", None) is not None:
            parts.append(f"[ImageContent {getattr(block, 'mime_type', 'image')}]")
    sc = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if sc:
        parts.append(json.dumps(sc, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


__all__ = [
    "ErrorKind",
    "build_server_env",
    "isolated_xdg_env",
    "resolve_iso",
    "seed_sessions",
]
