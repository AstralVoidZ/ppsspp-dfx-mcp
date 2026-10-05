"""opencode serve 生命周期管理。

对标 `porpoless/parse/attach.py`，沿用其所有权语义与安全加固。

## 所有权语义

- `ensure()` 探活顺序：显式 URL（env）→ 状态文件（HTTP 探活）→ 拉起新 server；
- 复用的 server **只使用不管理**；本管理器拉起的 server 由调用方 `shutdown()`；
- 拉起时生成随机密码注入 `OPENCODE_SERVER_PASSWORD`（server 与 client 同源）。

## 失败语义

连接失败不静默回退冷启动——由编排层报可操作错误（`ErrorKind.SERVER_GONE`）。

## 实现注意（实战踩中，继承 porpoless）

- **Windows/npm**：`Popen` 不解析 `.CMD`，`bin_path` 必须 `shutil.which` 到全路径；
- **探活以 HTTP 为准**：`Popen` 拿到的 pid 是 CMD wrapper（可能已退出），
  node 子进程才是服务——pid 仅用于 shutdown，不用于存活判断；
- **禁重定向**：Basic Auth 凭据不得跟随 30x 出域。

## 与旧实现的关键差异

旧 `opencode_collect.ServeManager` 无密码、无状态文件、无端口重探、无 URL 白名单，
且 shutdown 只 `terminate()` 就宣称完成。本模块补齐全部四项。
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from evals.oc.errors import CollectorError, ErrorKind
from evals.oc.log import get_logger

_log = get_logger("evals.oc.attach")

#: 受管 server 状态文件（跨 CLI 调用复用，跨进程共享）
STATE_FILE = Path(tempfile.gettempdir()) / "ppsspp_dfx_evals_oc_serve.json"
#: 外部 server 显式 URL 的环境变量
ATTACH_ENV = "PPSSPP_DFX_EVALS_OC_SERVE_URL"
#: 客户端侧密码的环境变量（与 server 同源）
PASSWORD_ENV = "OPENCODE_SERVER_PASSWORD"

STARTUP_TIMEOUT_S = 60
STARTUP_POLL_S = 0.5
HEALTH_TIMEOUT_S = 3
PORT_START = 4911
PORT_TRIES = 20

# 关闭后端口重探：确认 node 子进程确实随 CMD wrapper 终止
_SHUTDOWN_REPROBE_ATTEMPTS = 3
_SHUTDOWN_REPROBE_INTERVAL_S = 1.0
_SHUTDOWN_REPROBE_TIMEOUT_S = 1


@dataclass
class ServeHandle:
    url: str
    pid: int | None
    owned: bool  # 是否由本管理器拉起（决定调用方是否可关闭）
    password: str | None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁用重定向——Basic Auth 凭据不得跟随 30x 出域。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def http_ok(url: str, password: str | None, timeout: int = HEALTH_TIMEOUT_S) -> bool:
    """HTTP 探活。带密码时以 Basic 头携带（无密码的 server 会忽略该头）。"""
    req = urllib.request.Request(url)
    if password:
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return False


def validate_attach_url(url: str) -> str:
    """attach URL 白名单：仅允许本机 http 地址。

    opencode 会把完整 prompt（含场景卡、可能的真实游戏标识）发给 server，任意
    host 均可接收——因此显式 URL 只接受 `http://127.0.0.1:*` 与
    `http://localhost:*`。非法抛 `CollectorError`（`ErrorKind.BAD_URL`）。
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "http" or host not in ("127.0.0.1", "localhost"):
        raise CollectorError(
            f"attach URL 仅允许本机 http 地址（http://127.0.0.1:* / http://localhost:*），"
            f"收到: {url}——场景内容会完整发往该地址，禁止外部主机。",
            ErrorKind.BAD_URL,
        )
    return url


def pick_free_port(start: int = PORT_START, tries: int = PORT_TRIES) -> int:
    """从 start 起探测空闲端口（自动拉起需要显式端口以便构造 URL）。"""
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise CollectorError(f"{start} 起 {tries} 个端口均被占用", ErrorKind.SPAWN_FAILED)


def _load_state() -> dict | None:
    if not STATE_FILE.is_file():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _restrict_state_file_acl(path: Path) -> bool:
    """Windows 下收紧状态文件 ACL 为仅当前用户；返回是否成功。

    调用方须消费返回值并在 False 时告警——静默 return 会让失败不可观测。
    非 Windows 返回 True（不触发告警）。
    """
    if os.name != "nt":
        return True
    import getpass

    user = os.environ.get("USERNAME", "")
    # USERNAME 可被父进程污染：与 getpass 交叉校验且拒绝空白，防止把文件
    # 授权给错误主体（如 Everyone）
    if not user or user != user.strip() or user != getpass.getuser():
        return False
    try:
        proc = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return proc.returncode == 0
    except Exception as exc:  # noqa: BLE001 — ACL 尽力而为，不阻断主流程
        _log.debug("icacls 失败 %s: %s", path, exc)
        return False


def _save_state(state: dict) -> None:
    text = json.dumps(state, ensure_ascii=False)
    if os.name == "nt":
        # 先以受限 ACL 建空文件再写入——避免「明文先落盘」的竞态窗口
        STATE_FILE.touch(exist_ok=True)
        if not _restrict_state_file_acl(STATE_FILE):
            _log.warning("状态文件 ACL 收紧失败，server 密码可能对本机其他用户可见: %s", STATE_FILE)
        STATE_FILE.write_text(text, encoding="utf-8")
    else:
        fd = os.open(STATE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)


def _clear_state() -> None:
    STATE_FILE.unlink(missing_ok=True)


def client_password(url: str | None) -> str | None:
    """client 侧密码解析：显式传入 URL 匹配状态文件 → 其密码；否则 env。"""
    if url:
        state = _load_state()
        if state and state.get("url") == url:
            return state.get("password")
    return os.environ.get(PASSWORD_ENV)


def resolve_bin(explicit: str | None = None) -> str:
    """解析 opencode 可执行文件全路径。

    顺序：显式参数 → env `PPSSPP_DFX_EVALS_OC_BIN` → PATH → 裸名。

    旧实现硬编码了 pnpm global `v11/<hash>` 目录作为兜底——该目录名随每次
    pnpm 安装变化，版本漂移即失效。改为只信任 PATH 与显式配置，找不到时抛可
    操作错误，由调用方决定是否降级。
    """
    for cand in (explicit, os.environ.get("PPSSPP_DFX_EVALS_OC_BIN"), "opencode"):
        if not cand:
            continue
        found = shutil.which(cand)
        if found:
            return found
        # 已是全路径且存在（Popen 不解析 .CMD，故必须全路径）
        if os.path.isfile(cand):
            return cand
    return "opencode"


class ServeManager:
    """opencode serve 生命周期管理。"""

    def __init__(
        self,
        bin_path: str | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        startup_timeout_s: int = STARTUP_TIMEOUT_S,
    ) -> None:
        self.bin_path = resolve_bin(bin_path)
        self._sleeper = sleeper
        self._startup_timeout_s = startup_timeout_s

    def status(self) -> dict:
        """状态文件 + 实际探活 → 人读状态（探活以 HTTP 为准）。"""
        state = _load_state()
        if state is None:
            env_url = os.environ.get(ATTACH_ENV, "")
            if env_url:
                return {
                    "mode": "env-url",
                    "url": env_url,
                    "alive": http_ok(env_url, os.environ.get(PASSWORD_ENV)),
                }
            return {"mode": "none", "alive": False}
        alive = http_ok(state["url"], state.get("password"))
        return {
            "mode": "managed",
            "url": state["url"],
            "pid": state.get("pid"),
            "alive": alive,
            "started_at": state.get("started_at"),
            "stale": not alive,
        }

    def ensure(self, port: int | None = None) -> ServeHandle:
        """复用或拉起 server；返回 handle（`owned` 标记所有权）。"""
        env_url = os.environ.get(ATTACH_ENV, "").strip()
        if env_url:
            validate_attach_url(env_url)  # 防（误）配置把场景内容发往外部主机
            return ServeHandle(
                url=env_url,
                pid=None,
                owned=False,
                password=os.environ.get(PASSWORD_ENV),
            )

        state = _load_state()
        if state:
            # 状态文件与 env 同受白名单校验：共享 temp 下的固定名文件可被抢占，
            # 复用前不复验会把场景内容与密码发往外部主机
            try:
                validate_attach_url(state["url"])
            except CollectorError:
                _clear_state()
                state = None
        if state and http_ok(state["url"], state.get("password")):
            return ServeHandle(
                url=state["url"],
                pid=state["pid"],
                owned=False,
                password=state.get("password"),
            )
        if state:
            _clear_state()  # stale 状态文件

        return self._start(port or pick_free_port())

    def _start(self, port: int) -> ServeHandle:
        password = secrets.token_urlsafe(24)
        env = dict(os.environ)
        env[PASSWORD_ENV] = password
        cmd = [self.bin_path, "serve", "--port", str(port), "--hostname", "127.0.0.1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + self._startup_timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise CollectorError(
                    f"opencode serve 提前退出（code={proc.returncode}，bin={self.bin_path}）",
                    ErrorKind.SPAWN_FAILED,
                )
            if http_ok(url, password, timeout=2):
                state = {
                    "url": url,
                    "pid": proc.pid,
                    "password": password,
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                _save_state(state)
                return ServeHandle(url=url, pid=proc.pid, owned=True, password=password)
            self._sleeper(STARTUP_POLL_S)
        proc.terminate()
        raise CollectorError(
            f"opencode serve 启动超时（{self._startup_timeout_s}s）：{url}", ErrorKind.SPAWN_FAILED
        )

    def shutdown(self, handle: ServeHandle) -> bool:
        """仅对本管理器拉起且仍在状态的 server 执行关闭。

        pid 是 CMD wrapper（可能已退出）：terminate wrapper 后 node 子进程通常随
        会话终止，但**该行为不可靠**。故关闭后做一次有界重探，仍活则给出人工
        处置指引。
        """
        if not handle.owned:
            return False
        if handle.pid:
            try:
                os.kill(handle.pid, 15)
            except OSError as exc:
                _log.debug("terminate pid=%s 失败: %s", handle.pid, exc)
        _clear_state()
        self._confirm_port_closed(handle.url, handle.password)
        return True

    def _confirm_port_closed(self, url: str, password: str | None) -> None:
        """有界重探 url 是否仍在服务；仍在则给出人工处置指引。

        探活须带密码——否则 401 会被误判为「端口已关」。
        """
        for attempt in range(_SHUTDOWN_REPROBE_ATTEMPTS):
            if not http_ok(url, password, timeout=_SHUTDOWN_REPROBE_TIMEOUT_S):
                return
            if attempt < _SHUTDOWN_REPROBE_ATTEMPTS - 1:
                self._sleeper(_SHUTDOWN_REPROBE_INTERVAL_S)
        port = url.rsplit(":", 1)[-1]
        _log.warning(
            "shutdown 后 %s 仍可访问（端口 %s）——可能存在 node 孤儿进程"
            "（Windows 下 terminate 只杀 CMD wrapper）。\n"
            "  处置: netstat -ano | findstr :%s   取到 PID 后  taskkill /PID <pid> /F",
            url,
            port,
            port,
        )
