"""PPSSPP process launcher.

Ported from the original core_launcher.py module. CLI module removed
(not the MCP server's responsibility). PPSSPP exe path now read from
config (env > yaml), not from project_config module.

Random port strategy:
PPSSPP desktop builds do NOT parse ``--debugger=PORT`` on the command
line (only headless does — see open_source/ppsspp/headless/Headless.cpp).
The WebSocket debugger port is selected by ``g_Config.iRemoteISOPort``
in ``ppsspp.ini``. To override per-launch without polluting the user's
global ini, we write a temporary appended ini and pass it via
``--appendconfig=<path>`` (supported by desktop builds — see
open_source/ppsspp/UI/NativeApp.cpp:610-613).

Runtime port discovery:
On Windows desktop, ``--appendconfig=<path>`` is silently ignored
(only NativeApp.cpp parses it; Windows/main.cpp does not). PPSSPP
then uses the user's global ``iRemoteISOPort`` (often 0), which makes
``WebServer::ExecuteWebServer`` call ``Listen(0)`` — the OS assigns
a random ephemeral port (see open_source/ppsspp/Core/WebServer.cpp:
416-418). We discover that actual port post-launch by polling
``netstat -ano`` (Windows) / ``ss -tlnp`` (Linux) / ``lsof`` (macOS)
for the PPSSPP PID's listening sockets. ``self.ws_port`` is updated
to the discovered port; session_manager picks it up via
``launcher.ws_port`` (no API change).

Default behaviour: ``ws_port=None`` → pick a random free port on
``127.0.0.1`` per ``start()`` call. Callers wanting a fixed port can
pass ``ws_port=<n>`` explicitly (e.g. tests, or local debugging with a
known port). This eliminates the "12345 conflict" failure mode when
multiple PPSSPP instances run concurrently.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ppsspp_dfx_mcp.config import ppsspp_exe_path
from ppsspp_dfx_mcp.errors import IsoNotFound, PpssppNotFound

log = logging.getLogger(__name__)

DEFAULT_WS_PORT = 12345


def _default_ppsspp_exe_name() -> str:
    """Return the platform-appropriate default PPSSPP executable name."""
    if sys.platform == "win32":
        return "PPSSPPWindows64.exe"
    if sys.platform == "darwin":
        return "PPSSPPSDL"
    return "ppsspp"  # linux / other POSIX


def _force_kill_pid(pid: int) -> None:
    """Force-kill a process by PID (cross-platform).

    Windows: `taskkill /F /T /PID` kills the whole process tree.
    POSIX: `os.killpg` sends SIGKILL to the process group (best-effort;
    falls through silently if the process is already gone).
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=10,
        )
    else:
        import signal

        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(pid), signal.SIGKILL)  # already dead or not owned by us


def _pick_free_port(host: str = "127.0.0.1") -> int:
    """Return a free TCP port selected by the OS on ``host``.

    Uses ``socket.bind(("127.0.0.1", 0))`` so the OS picks an ephemeral
    port from the unprivileged range. The socket is closed before
    returning — there is a small TOCTOU window where another process
    could claim the port before PPSSPP binds it, but PPSSPP's
    ``StartWebServer`` falls back to port 0 if the chosen port is taken
    (see open_source/ppsspp/Core/WebServer.cpp:416-418), and our
    ``_wait_for_port`` polls for the actual listening port.

    Raises:
        OSError: if the socket cannot bind (extremely rare on localhost).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _write_appendconfig_ini(port: int) -> Path:
    """Write a temporary PPSSPP appended-config ini overriding the WS port.

    The ini is written to the system temp dir with a unique name
    (``ppsspp_dfx_debug_<pid>_<time>.ini``). ``PpssppLauncher.stop()``
    removes it on shutdown.

    Format (PPSSPP INI, section ``[SystemParam]``):
        [SystemParam]
        iRemoteISOPort = <port>
        bRemoteDebuggerOnStartup = True

    Returns:
        Path to the written ini file.
    """
    content = f"[SystemParam]\niRemoteISOPort = {port}\nbRemoteDebuggerOnStartup = True\n"
    # 内存断点在 JIT fastmem 直写下不触发（skill §4）：设 PPSSPP_DFX_IR=1
    # 时强制 CPUCore=2 解释器模式，专供 trace/breakpoint 调试会话。
    if os.environ.get("PPSSPP_DFX_IR"):
        content += "[General]\niCpuCore = 2\nbFastMemory = False\n"
    # Use both timestamp and uuid4 suffix to guarantee uniqueness even
    # when two calls land in the same millisecond on fast machines.
    import uuid

    name = f"ppsspp_dfx_debug_{os.getpid()}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.ini"
    tmp_dir = Path(tempfile.gettempdir())
    path = tmp_dir / name
    path.write_text(content, encoding="utf-8")
    return path


# ─── Runtime port discovery ────────────────────────────────────────────────
#
# When --appendconfig= is ignored (Windows desktop), PPSSPP falls back
# to its global iRemoteISOPort (often 0 → Listen(0) → OS-assigned port).
# We discover the actual listening port via netstat / ss / lsof so that
# session.ws_url reflects reality. Cross-platform helpers below.


def _parse_netstat_windows(output: str, pid: int) -> list[int]:
    """Parse Windows ``netstat -ano -p tcp`` output, returning listening
    TCP ports owned by ``pid``.

    Sample line:
        TCP    127.0.0.1:11242     0.0.0.0:0     LISTENING     12345
    """
    pid_str = str(pid)
    ports: list[int] = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if parts[0] != "TCP" or parts[3] != "LISTENING":
            continue
        if parts[4] != pid_str:
            continue
        local_addr = parts[1]
        if ":" not in local_addr:
            continue
        port_str = local_addr.rsplit(":", 1)[1]
        try:
            ports.append(int(port_str))
        except ValueError:
            continue
    return ports


def _parse_ss_or_netstat_posix(output: str, pid: int) -> list[int]:
    """Parse ``ss -tlnp`` or ``netstat -tlnp`` (Linux) output, returning
    listening TCP ports owned by ``pid``.

    ss format (Linux):
        LISTEN 0  4096  127.0.0.1:9490  0.0.0.0:*  users:(("ppsspp",pid=12345,fd=10))
    netstat format (Linux):
        tcp  0  0  127.0.0.1:9490  0.0.0.0:*  LISTEN  12345/ppsspp
    """
    pid_eq = f"pid={pid}"
    pid_slash = f"{pid}/"
    ports: list[int] = []
    for line in output.splitlines():
        if pid_eq not in line and pid_slash not in line:
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        # Both formats put local address in column index 3 (ss) or 3 (netstat).
        local_addr = parts[3]
        if ":" not in local_addr:
            continue
        port_str = local_addr.rsplit(":", 1)[1]
        try:
            ports.append(int(port_str))
        except ValueError:
            continue
    return ports


def _parse_lsof_posix(output: str) -> list[int]:
    """Parse ``lsof -iTCP -sTCP:LISTEN -P -n -p <pid>`` output (macOS).

    Sample line:
        COMMAND   PID    USER  NODE  NAME
        PPSSPP   12345  user   10u  IPv4  0x...  0t0  TCP *:9490 (LISTEN)
    """
    ports: list[int] = []
    for line in output.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 9:
            continue
        name = parts[8]  # NAME column
        if ":" not in name:
            continue
        # Strip "(LISTEN)" suffix and any trailing junk.
        port_str = name.rsplit(":", 1)[1].split()[0]
        try:
            ports.append(int(port_str))
        except ValueError:
            continue
    return ports


def _get_listening_ports_for_pid_windows(pid: int) -> list[int]:
    """Return listening TCP ports for ``pid`` on Windows (via netstat).

    Uses ``errors="replace"`` because ``netstat`` output is in the
    system's ANSI codepage (e.g. GBK on Chinese Windows), but Python
    in UTF-8 mode (``PYTHONUTF8=1``) decodes as UTF-8 by default.
    Without ``errors="replace"``, the reader thread raises
    UnicodeDecodeError and ``result.stdout`` becomes ``None``, causing
    ``_parse_netstat_windows`` to fail with AttributeError on
    ``None.splitlines()``.
    """
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
    except subprocess.SubprocessError, OSError:
        return []
    return _parse_netstat_windows(result.stdout or "", pid)


def _get_listening_ports_for_pid_posix(pid: int) -> list[int]:
    """Return listening TCP ports for ``pid`` on POSIX (Linux/macOS).

    Tries ``ss`` (modern Linux) → ``netstat`` (legacy Linux) → ``lsof``
    (macOS). Returns the first non-empty result.

    Uses ``errors="replace"`` for the same reason as the Windows variant:
    subprocess output may be in a locale-specific encoding, and
    ``PYTHONUTF8=1`` makes the default decode UTF-8, which can fail on
    non-UTF-8 locales. See ``_get_listening_ports_for_pid_windows``.
    """
    for cmd in (["ss", "-tlnp"], ["netstat", "-tlnp"]):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
            )
        except FileNotFoundError, subprocess.SubprocessError, OSError:
            continue
        ports = _parse_ss_or_netstat_posix(result.stdout or "", pid)
        if ports:
            return ports
    # macOS doesn't have ss/netstat with -p; use lsof.
    try:
        result = subprocess.run(
            ["lsof", "-a", "-iTCP", "-sTCP:LISTEN", "-P", "-n", f"-p{pid}"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5,
        )
    except FileNotFoundError, subprocess.SubprocessError, OSError:
        return []
    return _parse_lsof_posix(result.stdout or "")


def _get_listening_ports_for_pid(pid: int) -> list[int]:
    """Return listening TCP ports for ``pid`` (cross-platform)."""
    if sys.platform == "win32":
        return _get_listening_ports_for_pid_windows(pid)
    return _get_listening_ports_for_pid_posix(pid)


def _discover_listening_port_for_pid(
    pid: int,
    timeout: float = 5.0,
) -> int | None:
    """Poll until ``pid`` has a listening TCP port, or timeout.

    Used to discover PPSSPP's actual WebSocket debugger port when
    ``--appendconfig=`` is ignored (Windows desktop builds). PPSSPP
    falls back to ``Listen(0)`` on bind failure, getting an
    OS-assigned port (see open_source/ppsspp/Core/WebServer.cpp:
    416-418). Returns the first discovered port, or ``None`` if no
    port is found within ``timeout`` seconds.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        ports = _get_listening_ports_for_pid(pid)
        if ports:
            return ports[0]
        time.sleep(0.5)
    return None


class PpssppLauncher:
    """PPSSPP process manager.

    Encapsulates start/stop PPSSPP, port listening check, WebSocket ready wait.
    All paths use Path lib for auto-resolution (no hardcoded concatenation).

    Port selection:
    - ``ws_port=None`` (default): pick a random free port per ``start()``
      call. Eliminates the 12345 conflict failure mode.
    - ``ws_port=<int>``: use the specified fixed port. Useful for tests
      or when the user has a known-good port.

    The chosen port is exposed as ``self.ws_port`` after ``start()``
    (pre-``start()`` it is ``None`` when random mode was requested).
    """

    def __init__(
        self,
        exe_path: Path | None = None,
        ws_port: int | None = None,
        log_path: Path | None = None,
    ):
        if exe_path is not None:
            self.exe_path = Path(exe_path).resolve()
        else:
            resolved = ppsspp_exe_path()
            if resolved is None:
                # Platform-appropriate default; caller should raise if missing.
                self.exe_path = Path(_default_ppsspp_exe_name())
            else:
                self.exe_path = resolved
        # None = random port per start(); int = fixed port.
        self.ws_port: int | None = ws_port
        self.log_path = log_path
        self._proc: subprocess.Popen | None = None
        # Track the appendconfig ini so we can clean it up on stop().
        self._appendconfig_path: Path | None = None

    async def start(
        self,
        iso_path: Path,
        wait_seconds: float = 5.0,
        windowed: bool = True,
        extra_args: list | None = None,
    ) -> subprocess.Popen:
        """Start PPSSPP loading an ISO (async).

        Synchronous portions (subprocess spawn + port polling) are wrapped
        via `asyncio.to_thread` so the MCP server event loop is not
        blocked during `wait_seconds` (default 5s) of port polling.

        Args:
            iso_path: ISO file path.
            wait_seconds: Seconds to wait after launch (for WebSocket readiness).
            windowed: True=show window, False=no window. PPSSPP loading ISO
                requires the graphics subsystem — must use windowed mode.
            extra_args: Extra command-line args (e.g. --loadstate=path).

        Returns:
            subprocess.Popen process object.

        Raises:
            PpssppNotFound: PPSSPP executable does not exist.
            IsoNotFound: ISO file does not exist.
        """
        iso_path = Path(iso_path).resolve()
        if not self.exe_path.is_file():
            raise PpssppNotFound(f"PPSSPP executable not found: {self.exe_path}")
        if not iso_path.is_file():
            raise IsoNotFound(f"ISO file not found: {iso_path}")

        # Pick a port: random if self.ws_port is None.
        if self.ws_port is None:
            self.ws_port = _pick_free_port()

        # Clear old log (to distinguish this run's log).
        if self.log_path and self.log_path.is_file():
            with contextlib.suppress(OSError):
                self.log_path.unlink()

        # Write appendconfig ini and pass --appendconfig=<path> to PPSSPP.
        # Desktop PPSSPP does not parse --debugger=PORT (only headless does).
        # See open_source/ppsspp/UI/NativeApp.cpp:610-613.
        self._appendconfig_path = _write_appendconfig_ini(self.ws_port)

        cmd = [str(self.exe_path), f"--appendconfig={self._appendconfig_path}"]
        if extra_args:
            cmd.extend(extra_args)
        cmd.append(str(iso_path))

        # PPSSPP loading ISO requires graphics subsystem; default windowed.
        # CREATE_NO_WINDOW causes CPU not started, game=null, PC=0x00000000.
        creationflags = (
            0 if windowed else (subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        )
        # cwd set to PPSSPP exe dir for portable mode (memstick/ lookup).
        ppsspp_cwd = self.exe_path.parent

        # subprocess.Popen is blocking-ish but very short; run in thread
        # to keep event loop free. _wait_for_port polls with time.sleep,
        # so it MUST run in a thread (else it blocks the event loop).
        def _spawn_and_wait() -> subprocess.Popen:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Make PPSSPP the leader of a new session/process-group on
                # POSIX (start_new_session=True ⇒ setsid()). This is required
                # for _force_kill_pid's POSIX path, which calls
                # os.killpg(os.getpgid(pid), SIGKILL) — without this, PPSSPP
                # stays in the MCP server's process group and force-kill would
                # take the server down too. On Windows, start_new_session is
                # ignored entirely (CPython's _execute_child receives it as
                # unused_start_new_session and does NOT map it to any Win32
                # flag), so it is effectively a no-op on Windows.
                start_new_session=True,
                creationflags=creationflags,
                cwd=str(ppsspp_cwd),
            )
            self._proc = proc
            if wait_seconds > 0:
                self._wait_for_port(wait_seconds)
            return proc

        return await asyncio.to_thread(_spawn_and_wait)

    def _wait_for_port(self, timeout: float = 10.0) -> bool:
        """Wait for WebSocket port to be listening, with runtime discovery.

        Phase 1 (fast path, half the timeout): poll ``self.ws_port``
        directly — works when ``--appendconfig=`` takes effect (Linux
        desktop) and PPSSPP binds the port we requested.

        Phase 2 (fallback, remaining timeout): discover the PID's
        actual listening TCP port via ``netstat`` / ``ss`` / ``lsof``
        — used when ``--appendconfig=`` is ignored (Windows desktop,
        see open_source/ppsspp/Windows/main.cpp) and PPSSPP falls
        back to ``Listen(0)`` (open_source/ppsspp/Core/WebServer.cpp:
        416-418). Updates ``self.ws_port`` if the discovered port
        differs from the picked port. session_manager picks up the
        updated value automatically (no API change).

        Returns:
            True if ``self.ws_port`` is confirmed listening.
        """
        if self._proc is None or self.ws_port is None:
            return False

        # Phase 1: poll picked port briefly.
        phase1_timeout = timeout / 2
        phase1_deadline = time.time() + phase1_timeout
        while time.time() < phase1_deadline:
            if self._is_port_listening():
                return True
            if self._proc.poll() is not None:
                # Process died; no point waiting further.
                return False
            time.sleep(0.3)

        # Phase 2: discover actual listening port for the PID.
        if self._proc.pid <= 0:
            return False
        discovered = _discover_listening_port_for_pid(
            self._proc.pid,
            timeout=phase1_timeout,
        )
        if discovered is None:
            return False
        if discovered != self.ws_port:
            log.info(
                "PPSSPP listening on discovered port %d (picked %d; "
                "--appendconfig= likely ignored on this platform)",
                discovered,
                self.ws_port,
            )
            self.ws_port = discovered
        return self._is_port_listening()

    def _is_port_listening(self) -> bool:
        """Check if WebSocket port is listening."""
        if self.ws_port is None:
            return False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex(("127.0.0.1", self.ws_port))
            sock.close()
            return result == 0
        except Exception:
            return False

    def stop(self) -> None:
        """Stop the PPSSPP process gracefully (terminate → kill → force-kill).

        Also cleans up the temporary appendconfig ini file written in
        ``start()`` (if any) and resets ``ws_port`` to None so the
        launcher can be reused with a fresh port on the next ``start()``.
        """
        if self._proc is not None:
            if self._proc.poll() is None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        self._proc.kill()
                        self._proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        _force_kill_pid(self._proc.pid)
            self._proc = None
        # Clean up the appendconfig ini (best-effort; ignore errors).
        if self._appendconfig_path is not None:
            try:
                if self._appendconfig_path.is_file():
                    self._appendconfig_path.unlink()
            except OSError:
                pass
            self._appendconfig_path = None
        # Reset ws_port so a subsequent start() picks a fresh port.
        self.ws_port = None

    @property
    def is_running(self) -> bool:
        """Whether the PPSSPP process is running."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        """Process PID (None if not started)."""
        return self._proc.pid if self._proc else None

    def read_log(self) -> str:
        """Read the PPSSPP log file contents.

        Returns:
            Log text (empty string if file does not exist).
        """
        if self.log_path is None or not self.log_path.is_file():
            return ""
        return self.log_path.read_text(encoding="utf-8", errors="replace")
