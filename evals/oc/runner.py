"""opencode Harness CLI 驱动。

对标 `porpoless/parse/cli_runner.py`（v1.18.30 基线），按本机 **opencode v2.0.19**
实测差异做了两处必要偏离。

## 偏离 1：flag 能力探测（跨版本兼容闸门）

porpoless 的命令模板为
`opencode run --format json -m <model> --auto --pure --dir <dir> --agent <a> --title <t>`，
attach 用 `--attach`。本机 v2.0.19 `opencode run --help` 实测：

- `--attach` **不存在**，取而代之的是 `--server`；
- `--dir` **不存在**（工作目录改由 Popen `cwd=` 决定）；
- `--pure` **不存在**；
- `--format json` / `--auto` / `--agent` / `--title` / `-m` / `-f` 仍在。

直接照抄 porpoless 的模板会在 v2 上因未知 flag 整体失败。因此本模块在
`supported_flags()` 里做一次能力探测（**合并 stdout+stderr** 后匹配——实测
`opencode run --help` 正文走 stderr，stdout 恒空，只查 stdout 会永远探测不到），
`_build_command` 只拼装被支持的 flag。v1 / v2 双端可用。

## 偏离 2：超时安全

旧 `opencode_collect.run_scenario` 用 `for line in proc.stdout` 先读完再
`proc.wait(timeout=...)`——**timeout 形同虚设**：opencode 挂死时读取循环
永久阻塞，硬顶永不触发。`RUN_TIMEOUT_S=600` 因此从未真正生效过。

本模块改为「读取线程 + `wait(timeout=)` 强杀」：线程只负责排空管道并转发
进度行，deadline 由 `wait()` 单点裁决，超时则 `kill()` 后回收线程。

## 沿用 porpoless 的加固

- `--share` 族数据外发禁用（沙箱断言 H7c）——任何形态都不得进入命令行；
- 显式 `-m` 是防卡死的必要条件：无模型时 opencode 在非交互模式永久阻塞；
- agent 可发现性 fail fast；
- `database is locked` 退避重试；
- 采集热路径的输出走 progress / logger，**不直写 stdout**。
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from evals.oc import progress
from evals.oc.errors import ErrorKind
from evals.oc.log import get_logger

_log = get_logger("evals.oc.runner")

NAME = "opencode"

#: 数据外发禁用旗标（含短名与同义变形）：任何形态都不得进入命令行
_SEND_OUT_FLAGS = frozenset({"--share", "-s"})

_FLAG_RE = re.compile(r"(?m)^\s{2,}(-{1,2}[A-Za-z][\w-]*)(?:,\s*(-{1,2}[A-Za-z][\w-]*))?")


def _guard_no_share(cmd: list[str]) -> None:
    """数据外发禁用：任何 `--share` 变形（`--share` / `--share=v`）都拦截。"""
    for token in cmd:
        if token.split("=", 1)[0] in _SEND_OUT_FLAGS:
            raise ValueError(f"数据外发禁用：{token!r} 不得进入 opencode 命令行（沙箱断言 H7c）")


@dataclass
class OpencodeRun:
    """一次 `opencode run` 子进程调用的结果。"""

    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    timed_out: bool = False
    duration_s: float = 0.0
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and bool(self.stdout.strip())


@dataclass
class RunOptions:
    """单次调用的输入（与 porpoless 的 LLMRequest 对位）。"""

    prompt: str
    title: str | None = None
    #: 逐行进度回调（事件流原文，已截断）；None 则不回调
    on_line: Callable[[str], None] | None = None
    #: 事件流落盘路径；给定则写入完整 stdout
    dump_path: Path | None = None


@dataclass
class _CommandPlan:
    """命令构造结果，附带「哪些 flag 被能力探测否决」的诊断。"""

    cmd: list[str]
    dropped_flags: list[str] = field(default_factory=list)


class CliRunner:
    """opencode `run` 子命令驱动。"""

    name = NAME

    def __init__(
        self,
        bin_path: str | None = None,
        *,
        model: str | None = None,
        agent: str | None = None,
        work_dir: str | None = None,
        title_prefix: str = "ppsspp-dfx",
        auto: bool = True,
        pure: bool = False,
        use_dir_flag: bool = False,
        server_url: str | None = None,
        server_password: str | None = None,
        timeout_s: int = 600,
        max_retries: int = 2,
        retry_backoff_s: float = 5.0,
        extra_args: Iterable[str] = (),
        env_overrides: dict[str, str] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        # Windows/npm：Popen 不解析 .CMD——必须 which 到全路径
        resolved = bin_path or os.environ.get("PPSSPP_DFX_EVALS_OC_BIN") or "opencode"
        self.bin_path = shutil.which(resolved) or resolved
        # 显式 -m 是防卡死的必要条件：无模型时 opencode 在非交互模式永久阻塞
        self.model = model or os.environ.get("PPSSPP_DFX_EVALS_OC_MODEL") or ""
        self.agent = agent
        self.work_dir = work_dir
        self.title_prefix = title_prefix
        self.auto = auto
        self.pure = pure
        self.use_dir_flag = use_dir_flag
        self.server_url = server_url
        self.server_password = server_password
        self.timeout_s = timeout_s
        self.max_retries = max(0, max_retries)
        self.retry_backoff_s = retry_backoff_s
        self.extra_args = list(extra_args)
        self.env_overrides = dict(env_overrides or {})
        self._sleeper = sleeper
        self._supported_flags: frozenset[str] | None = None

    # ── 能力探测 ───────────────────────────────────────────────────────

    def supported_flags(self, *, refresh: bool = False) -> frozenset[str]:
        """探测当前 opencode `run` 支持的 flag（缓存）。

        合并 stdout+stderr 后匹配——实测 `run --help` 正文走 stderr。
        探测失败按「保守集」处理：只拼装两版本都存在的 flag。
        """
        if self._supported_flags is not None and not refresh:
            return self._supported_flags
        try:
            proc = subprocess.run(
                [self.bin_path, "run", "--help"],
                capture_output=True,
                timeout=60,
                encoding="utf-8",
                errors="replace",
            )
            blob = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except (OSError, subprocess.TimeoutExpired) as exc:
            _log.warning("opencode run --help 探测失败（%s），退回保守 flag 集", exc)
            self._supported_flags = frozenset(_CONSERVATIVE_FLAGS)
            return self._supported_flags
        found = {m.group(1) for m in _FLAG_RE.finditer(blob)}
        found |= {m.group(2) for m in _FLAG_RE.finditer(blob) if m.group(2)}
        if not found:
            _log.warning("opencode run --help 未解析出 flag，退回保守 flag 集")
            found = set(_CONSERVATIVE_FLAGS)
        _log.debug("opencode run 支持 flag: %s", sorted(found))
        self._supported_flags = frozenset(found)
        return self._supported_flags

    def agent_error(self) -> str | None:
        """agent 可发现性校验：返回错误消息或 None（fail fast 用）。"""
        if not self.agent:
            return None
        candidates = []
        if self.work_dir:
            candidates.append(Path(self.work_dir) / ".opencode" / "agents" / f"{self.agent}.md")
        candidates.append(Path.home() / ".config" / "opencode" / "agents" / f"{self.agent}.md")
        if any(p.is_file() for p in candidates):
            return None
        return (
            f"agent {self.agent!r} 未找到（查找: "
            f"{[str(p) for p in candidates]}）。"
            f"请检查 .opencode/agents/ 下随仓库分发的定义文件，或换 --oc-agent。"
        )

    # ── 命令构造 ────────────────────────────────────────────────────────

    def _server_flag(self, supported: frozenset[str]) -> str | None:
        """attach 到常驻 server 的 flag 名：v1 `--attach` / v2 `--server`。"""
        for flag in ("--server", "--attach"):
            if flag in supported:
                return flag
        return None

    def build_command(self, options: RunOptions) -> _CommandPlan:
        """构造 `opencode run` 命令行（只拼装被支持的 flag）。"""
        supported = self.supported_flags()
        dropped: list[str] = []
        cmd = [self.bin_path, "run"]
        if "--format" in supported:
            cmd += ["--format", "json"]
        if self.server_url:
            flag = self._server_flag(supported)
            if flag:
                cmd += [flag, self.server_url]
            else:
                dropped.append("--server/--attach")
        if self.model:
            cmd += ["-m", self.model]
        if self.auto and "--auto" in supported:
            cmd.append("--auto")
        if self.pure:
            if "--pure" in supported:
                cmd.append("--pure")
            else:
                dropped.append("--pure")
        if self.use_dir_flag and self.work_dir:
            if "--dir" in supported:
                cmd += ["--dir", self.work_dir]
            else:
                dropped.append("--dir")
        if self.agent and "--agent" in supported:
            cmd += ["--agent", self.agent]
        title = options.title
        if title and "--title" in supported:
            cmd += ["--title", title]
        cmd.extend(self.extra_args)
        if dropped:
            _log.info(
                "当前 opencode 不支持 %s，已忽略（干净环境由 auto/agent/cwd 保证）",
                ", ".join(dropped),
            )
        _guard_no_share(cmd)
        return _CommandPlan(cmd=cmd, dropped_flags=dropped)

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.server_url and self.server_password:
            env["OPENCODE_SERVER_PASSWORD"] = self.server_password
        env.update(self.env_overrides)
        return env

    # ── 执行 ────────────────────────────────────────────────────────────

    @staticmethod
    def _is_retryable_output(stdout: str, stderr: str) -> bool:
        """opencode 输出中的可重试错误：内部状态库锁。"""
        return "database is locked" in (stdout + stderr).lower()

    def _run_once(self, plan: _CommandPlan, options: RunOptions) -> OpencodeRun:
        """跑一次子进程。deadline 由 `wait(timeout=)` 单点裁决。"""
        t0 = time.monotonic()
        # text=True → stdin 是**文本**流，写 bytes 会 TypeError（实测踩中）
        stdin_payload = options.prompt
        try:
            proc = subprocess.Popen(
                plan.cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._environment(),
                cwd=self.work_dir or None,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            raise OSError(f"无法拉起 opencode（bin={self.bin_path}）：{exc}") from exc

        out_parts: list[str] = []
        err_parts: list[str] = []

        def _pump(handle: object, sink: list[str], on_line: Callable[[str], None] | None) -> None:
            """排空一个管道到内存缓冲（可选逐行回调）。线程不裁决超时。"""
            if handle is None:
                return
            for line in handle:  # type: ignore[union-attr]
                sink.append(line)
                if on_line is not None:
                    on_line(line.rstrip())

        out_thread = threading.Thread(
            target=_pump, args=(proc.stdout, out_parts, options.on_line), daemon=True
        )
        err_thread = threading.Thread(
            target=_pump, args=(proc.stderr, err_parts, None), daemon=True
        )

        def _feed_stdin() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(stdin_payload)
            except (BrokenPipeError, OSError) as exc:
                # 子进程提前退出（参数错/认证失败）时写 stdin 会 EPIPE——
                # 真实原因在 stderr，这里只留痕不抛，避免掩盖根因。
                _log.debug("写 stdin 失败（子进程可能已退出）: %s", exc)
            finally:
                stream_in = proc.stdin
                if stream_in is not None:
                    with contextlib.suppress(OSError):
                        stream_in.close()

        in_thread = threading.Thread(target=_feed_stdin, daemon=True)
        in_thread.start()
        out_thread.start()
        err_thread.start()

        timed_out = False
        try:
            proc.wait(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _log.warning(
                "opencode run 超时 %ss，强杀（cmd=%s）", self.timeout_s, " ".join(plan.cmd)
            )
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover — kill 后极少再挂
                _log.warning("强杀后进程仍未退出，放弃回收等待")
        for t in (in_thread, out_thread, err_thread):
            t.join(timeout=15)

        stdout = "".join(out_parts)
        stderr = "".join(err_parts)
        if options.dump_path is not None:
            options.dump_path.parent.mkdir(parents=True, exist_ok=True)
            options.dump_path.write_text(stdout, encoding="utf-8")
        if stderr.strip():
            progress.emit(f"  [stderr] {stderr.strip()[:200]}", min_level=2)
        return OpencodeRun(
            stdout=stdout,
            stderr=stderr,
            returncode=proc.returncode if proc.returncode is not None else -1,
            timed_out=timed_out,
            duration_s=round(time.monotonic() - t0, 2),
        )

    def run(self, options: RunOptions) -> OpencodeRun:
        """驱动一次采集调用（含锁竞争退避重试）。"""
        agent_err = self.agent_error()
        if agent_err:
            raise ValueError(agent_err)
        plan = self.build_command(options)
        last_retryable = False
        attempt = 0
        result = OpencodeRun()
        for attempt in range(self.max_retries + 1):
            result = self._run_once(plan, options)
            result.attempts = attempt + 1
            # locked 输出可能非空（ANSI 错误页），不能以 stdout 为空为前提
            if (
                self._is_retryable_output(result.stdout, result.stderr)
                and attempt < self.max_retries
            ):
                last_retryable = True
                _log.warning(
                    "opencode 内部状态库锁竞争，退避重试（%d/%d）", attempt + 1, self.max_retries
                )
                self._sleeper(self.retry_backoff_s)
                continue
            break
        if last_retryable and not result.stdout.strip():
            _log.debug("锁竞争持续至重试耗尽")
        return result

    def classify(self, run: OpencodeRun) -> tuple[str, str]:
        """把子进程结果归为 (error_kind, 人读消息)；成功返回 ("", "")。"""
        if run.timed_out:
            return (
                ErrorKind.TIMEOUT,
                f"opencode 超时 {self.timeout_s}s 被强杀（exit={run.returncode}）",
            )
        if run.returncode != 0 or not run.stdout.strip():
            detail = run.stderr.strip()[:300] or f"exit={run.returncode}"
            kind = (
                ErrorKind.LOCKED
                if self._is_retryable_output(run.stdout, run.stderr)
                else ErrorKind.EMPTY_OUTPUT
            )
            if self.server_url:
                kind = ErrorKind.SERVER_GONE
            return kind, f"opencode 无输出（{detail}）"
        return "", ""


#: 探测失败时的保守 flag 集（v1 / v2 均存在）
_CONSERVATIVE_FLAGS = frozenset({"--format", "--auto", "--agent", "--title", "-m"})
