"""Infrastructure-layer tests: PpssppLauncher (core/launcher.py).

Tests the PPSSPP process launcher in isolation — no real PPSSPP spawned.
All subprocess / socket / port-discovery calls are mocked.

Covers:
- Module-level helpers: _default_ppsspp_exe_name, _pick_free_port,
  _write_appendconfig_ini, _parse_netstat_windows,
  _parse_ss_or_netstat_posix, _parse_lsof_posix.
- PpssppLauncher.__init__: exe_path resolution (explicit / config / platform default).
- PpssppLauncher.start: PpssppNotFound / IsoNotFound / success path
  (cmd construction, Popen call, _pick_free_port trigger, extra_args,
  log cleanup, _appendconfig_path set).
- PpssppLauncher._wait_for_port: Phase 1 fast path / process died /
  Phase 2 discover / Phase 2 timeout / guards.
- PpssppLauncher._is_port_listening: None port / listening / not listening.
- PpssppLauncher.stop: no-op / terminate / kill / force-kill / appendconfig
  cleanup / ws_port reset.
- Properties: is_running / pid / read_log.

Skipped (low value, high mock cost): _force_kill_pid,
_get_listening_ports_for_pid_windows|posix (subprocess.run + parse
composition; parse tested separately), _discover_listening_port_for_pid
(trivial polling loop).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ppsspp_dfx_mcp.core.launcher import (
    PpssppLauncher,
    _default_ppsspp_exe_name,
    _parse_lsof_posix,
    _parse_netstat_windows,
    _parse_ss_or_netstat_posix,
    _pick_free_port,
    _write_appendconfig_ini,
)
from ppsspp_dfx_mcp.errors import IsoNotFound, PpssppNotFound

# ============================================================================
# Module-level helpers
# ============================================================================


class TestDefaultPpssppExeName:
    """_default_ppsspp_exe_name returns platform-appropriate exe name."""

    def test_windows(self):
        with patch("sys.platform", "win32"):
            assert _default_ppsspp_exe_name() == "PPSSPPWindows64.exe"

    def test_macos(self):
        with patch("sys.platform", "darwin"):
            assert _default_ppsspp_exe_name() == "PPSSPPSDL"

    def test_linux(self):
        with patch("sys.platform", "linux"):
            assert _default_ppsspp_exe_name() == "ppsspp"


class TestPickFreePort:
    """_pick_free_port returns a valid OS-assigned port."""

    def test_returns_positive_int(self):
        port = _pick_free_port()
        assert isinstance(port, int)
        assert port > 0

    def test_returns_unprivileged_port(self):
        port = _pick_free_port()
        # Ephemeral range — well above privileged ports.
        assert port > 1024

    def test_two_calls_return_different_ports(self):
        # OS assigns distinct ephemeral ports on successive binds.
        p1 = _pick_free_port()
        p2 = _pick_free_port()
        assert p1 != p2


class TestWriteAppendconfigIni:
    """_write_appendconfig_ini writes a temp ini with the WS port."""

    def test_file_exists_in_tempdir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "ppsspp_dfx_mcp.core.launcher.tempfile.gettempdir", lambda: str(tmp_path)
        )
        path = _write_appendconfig_ini(12345)
        assert path.is_file()
        assert path.parent == tmp_path

    def test_filename_has_expected_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "ppsspp_dfx_mcp.core.launcher.tempfile.gettempdir", lambda: str(tmp_path)
        )
        path = _write_appendconfig_ini(12345)
        assert path.name.startswith("ppsspp_dfx_debug_")
        assert path.suffix == ".ini"

    def test_content_has_general_section(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "ppsspp_dfx_mcp.core.launcher.tempfile.gettempdir", lambda: str(tmp_path)
        )
        path = _write_appendconfig_ini(9999)
        content = path.read_text(encoding="utf-8")
        assert "[General]" in content
        assert "RemoteISOPort = 9999" in content
        assert "RemoteDebuggerOnStartup = True" in content
        assert "RemoteDebuggerLocal = True" in content


class TestParseNetstatWindows:
    """_parse_netstat_windows extracts listening ports for a PID."""

    def test_normal_line_parsed(self):
        output = "  TCP    127.0.0.1:11242     0.0.0.0:0     LISTENING     12345\n"
        ports = _parse_netstat_windows(output, 12345)
        assert ports == [11242]

    def test_multiple_lines(self):
        output = (
            "  TCP    127.0.0.1:11242     0.0.0.0:0     LISTENING     12345\n"
            "  TCP    127.0.0.1:11243     0.0.0.0:0     LISTENING     12345\n"
        )
        ports = _parse_netstat_windows(output, 12345)
        assert ports == [11242, 11243]

    def test_non_listening_skipped(self):
        output = "  TCP    127.0.0.1:11242     10.0.0.1:80    ESTABLISHED   12345\n"
        ports = _parse_netstat_windows(output, 12345)
        assert ports == []

    def test_non_target_pid_skipped(self):
        output = "  TCP    127.0.0.1:11242     0.0.0.0:0     LISTENING     99999\n"
        ports = _parse_netstat_windows(output, 12345)
        assert ports == []

    def test_non_tcp_skipped(self):
        output = "  UDP    0.0.0.0:1234     *:*        12345\n"
        ports = _parse_netstat_windows(output, 12345)
        assert ports == []

    def test_malformed_line_skipped(self):
        output = "garbage line with no parts\n"
        ports = _parse_netstat_windows(output, 12345)
        assert ports == []

    def test_empty_output(self):
        assert _parse_netstat_windows("", 12345) == []


class TestParseSsOrNetstatPosix:
    """_parse_ss_or_netstat_posix extracts listening ports for a PID."""

    def test_ss_format_parsed(self):
        output = 'LISTEN 0  4096  127.0.0.1:9490  0.0.0.0:*  users:(("ppsspp",pid=12345,fd=10))\n'
        ports = _parse_ss_or_netstat_posix(output, 12345)
        assert ports == [9490]

    def test_netstat_format_parsed(self):
        output = "tcp  0  0  127.0.0.1:9490  0.0.0.0:*  LISTEN  12345/ppsspp\n"
        ports = _parse_ss_or_netstat_posix(output, 12345)
        assert ports == [9490]

    def test_non_target_pid_skipped(self):
        output = 'LISTEN 0  4096  127.0.0.1:9490  0.0.0.0:*  users:(("ppsspp",pid=99999,fd=10))\n'
        ports = _parse_ss_or_netstat_posix(output, 12345)
        assert ports == []

    def test_empty_output(self):
        assert _parse_ss_or_netstat_posix("", 12345) == []


class TestParseLsofPosix:
    """_parse_lsof_posix extracts listening ports (macOS)."""

    def test_normal_line_parsed(self):
        output = (
            "COMMAND   PID    USER  NODE  NAME\n"
            "PPSSPP   12345  user   10u  IPv4  0x...  0t0  TCP *:9490 (LISTEN)\n"
        )
        ports = _parse_lsof_posix(output)
        assert ports == [9490]

    def test_header_only_returns_empty(self):
        output = "COMMAND   PID    USER  NODE  NAME\n"
        assert _parse_lsof_posix(output) == []

    def test_empty_output(self):
        assert _parse_lsof_posix("") == []

    def test_malformed_line_skipped(self):
        output = "COMMAND   PID    USER  NODE  NAME\nshort\n"
        assert _parse_lsof_posix(output) == []


# ============================================================================
# PpssppLauncher.__init__
# ============================================================================


class TestInit:
    """PpssppLauncher.__init__ resolves exe_path and ws_port."""

    def test_explicit_exe_path_resolved(self, tmp_path):
        exe = tmp_path / "PPSSPPWindows64.exe"
        exe.write_bytes(b"\x00")
        launcher = PpssppLauncher(exe_path=exe)
        assert launcher.exe_path == exe.resolve()

    def test_config_returns_path(self, tmp_path):
        exe = tmp_path / "ppsspp"
        exe.write_bytes(b"\x00")
        with patch(
            "ppsspp_dfx_mcp.core.launcher.ppsspp_exe_path",
            return_value=exe,
        ):
            launcher = PpssppLauncher()
        assert launcher.exe_path == exe

    def test_config_none_uses_platform_default(self):
        with (
            patch(
                "ppsspp_dfx_mcp.core.launcher.ppsspp_exe_path",
                return_value=None,
            ),
            patch("sys.platform", "win32"),
        ):
            launcher = PpssppLauncher()
        assert launcher.exe_path == Path("PPSSPPWindows64.exe")

    def test_ws_port_none_preserved(self):
        launcher = PpssppLauncher()
        assert launcher.ws_port is None

    def test_ws_port_int_preserved(self):
        launcher = PpssppLauncher(ws_port=12345)
        assert launcher.ws_port == 12345

    def test_log_path_preserved(self, tmp_path):
        log = tmp_path / "ppsspp.log"
        launcher = PpssppLauncher(log_path=log)
        assert launcher.log_path == log

    def test_initial_state_no_proc(self):
        launcher = PpssppLauncher()
        assert launcher._proc is None
        assert launcher._appendconfig_path is None
        assert launcher.is_running is False
        assert launcher.pid is None


# ============================================================================
# PpssppLauncher.start
# ============================================================================


class TestStart:
    """PpssppLauncher.start: error guards + success path."""

    async def test_raises_ppsspp_not_found(self, tmp_path):
        exe = tmp_path / "nonexistent.exe"
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00")
        launcher = PpssppLauncher(exe_path=exe, ws_port=12345)
        with pytest.raises(PpssppNotFound, match="PPSSPP executable not found"):
            await launcher.start(iso)

    async def test_raises_iso_not_found(self, tmp_path):
        exe = tmp_path / "PPSSPP.exe"
        exe.write_bytes(b"\x00")
        iso = tmp_path / "missing.iso"
        launcher = PpssppLauncher(exe_path=exe, ws_port=12345)
        with pytest.raises(IsoNotFound, match="ISO file not found"):
            await launcher.start(iso)

    async def test_success_path_calls_popen(self, tmp_path):
        exe = tmp_path / "PPSSPP.exe"
        exe.write_bytes(b"\x00")
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00")
        launcher = PpssppLauncher(exe_path=exe, ws_port=12345)

        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 99999
        fake_proc.poll.return_value = None

        with (
            patch(
                "ppsspp_dfx_mcp.core.launcher.subprocess.Popen",
                return_value=fake_proc,
            ) as mock_popen,
            patch.object(launcher, "_wait_for_port", return_value=True),
            patch(
                "ppsspp_dfx_mcp.core.launcher._write_appendconfig_ini",
                return_value=tmp_path / "fake.ini",
            ),
        ):
            result = await launcher.start(iso)

        assert result is fake_proc
        assert launcher._proc is fake_proc
        # Popen called with cmd containing exe + appendconfig + iso.
        args, kwargs = mock_popen.call_args
        cmd = args[0] if args else kwargs.get("args")
        assert str(exe.resolve()) in cmd
        assert str(iso.resolve()) in cmd
        assert any("--appendconfig=" in c for c in cmd)

    async def test_ws_port_none_triggers_pick_free_port(self, tmp_path):
        exe = tmp_path / "PPSSPP.exe"
        exe.write_bytes(b"\x00")
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00")
        launcher = PpssppLauncher(exe_path=exe, ws_port=None)

        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 99999
        fake_proc.poll.return_value = None

        with (
            patch(
                "ppsspp_dfx_mcp.core.launcher.subprocess.Popen",
                return_value=fake_proc,
            ),
            patch.object(launcher, "_wait_for_port", return_value=True),
            patch(
                "ppsspp_dfx_mcp.core.launcher._write_appendconfig_ini",
                return_value=tmp_path / "fake.ini",
            ),
            patch(
                "ppsspp_dfx_mcp.core.launcher._pick_free_port",
                return_value=23456,
            ) as mock_pick,
        ):
            await launcher.start(iso)

        mock_pick.assert_called_once()
        assert launcher.ws_port == 23456

    async def test_ws_port_fixed_does_not_call_pick_free_port(self, tmp_path):
        exe = tmp_path / "PPSSPP.exe"
        exe.write_bytes(b"\x00")
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00")
        launcher = PpssppLauncher(exe_path=exe, ws_port=12345)

        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 99999
        fake_proc.poll.return_value = None

        with (
            patch(
                "ppsspp_dfx_mcp.core.launcher.subprocess.Popen",
                return_value=fake_proc,
            ),
            patch.object(launcher, "_wait_for_port", return_value=True),
            patch(
                "ppsspp_dfx_mcp.core.launcher._write_appendconfig_ini",
                return_value=tmp_path / "fake.ini",
            ),
            patch("ppsspp_dfx_mcp.core.launcher._pick_free_port") as mock_pick,
        ):
            await launcher.start(iso)

        mock_pick.assert_not_called()
        assert launcher.ws_port == 12345

    async def test_extra_args_appended_to_cmd(self, tmp_path):
        exe = tmp_path / "PPSSPP.exe"
        exe.write_bytes(b"\x00")
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00")
        launcher = PpssppLauncher(exe_path=exe, ws_port=12345)

        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 99999
        fake_proc.poll.return_value = None

        with (
            patch(
                "ppsspp_dfx_mcp.core.launcher.subprocess.Popen",
                return_value=fake_proc,
            ) as mock_popen,
            patch.object(launcher, "_wait_for_port", return_value=True),
            patch(
                "ppsspp_dfx_mcp.core.launcher._write_appendconfig_ini",
                return_value=tmp_path / "fake.ini",
            ),
        ):
            await launcher.start(iso, extra_args=["--loadstate=/tmp/state.sav"])

        args, _ = mock_popen.call_args
        cmd = args[0]
        assert "--loadstate=/tmp/state.sav" in cmd

    async def test_log_path_old_file_deleted(self, tmp_path):
        exe = tmp_path / "PPSSPP.exe"
        exe.write_bytes(b"\x00")
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00")
        log_path = tmp_path / "ppsspp.log"
        log_path.write_text("old log content", encoding="utf-8")
        launcher = PpssppLauncher(exe_path=exe, ws_port=12345, log_path=log_path)

        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 99999
        fake_proc.poll.return_value = None

        with (
            patch(
                "ppsspp_dfx_mcp.core.launcher.subprocess.Popen",
                return_value=fake_proc,
            ),
            patch.object(launcher, "_wait_for_port", return_value=True),
            patch(
                "ppsspp_dfx_mcp.core.launcher._write_appendconfig_ini",
                return_value=tmp_path / "fake.ini",
            ),
        ):
            await launcher.start(iso)

        assert not log_path.exists()

    async def test_appendconfig_path_set_after_start(self, tmp_path):
        exe = tmp_path / "PPSSPP.exe"
        exe.write_bytes(b"\x00")
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00")
        launcher = PpssppLauncher(exe_path=exe, ws_port=12345)
        fake_ini = tmp_path / "appended.ini"

        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 99999
        fake_proc.poll.return_value = None

        with (
            patch(
                "ppsspp_dfx_mcp.core.launcher.subprocess.Popen",
                return_value=fake_proc,
            ),
            patch.object(launcher, "_wait_for_port", return_value=True),
            patch(
                "ppsspp_dfx_mcp.core.launcher._write_appendconfig_ini",
                return_value=fake_ini,
            ),
        ):
            await launcher.start(iso)

        assert launcher._appendconfig_path == fake_ini

    async def test_wait_seconds_zero_skips_wait_for_port(self, tmp_path):
        exe = tmp_path / "PPSSPP.exe"
        exe.write_bytes(b"\x00")
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00")
        launcher = PpssppLauncher(exe_path=exe, ws_port=12345)

        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 99999
        fake_proc.poll.return_value = None

        with (
            patch(
                "ppsspp_dfx_mcp.core.launcher.subprocess.Popen",
                return_value=fake_proc,
            ),
            patch.object(launcher, "_wait_for_port") as mock_wait,
            patch(
                "ppsspp_dfx_mcp.core.launcher._write_appendconfig_ini",
                return_value=tmp_path / "fake.ini",
            ),
        ):
            await launcher.start(iso, wait_seconds=0)

        mock_wait.assert_not_called()


# ============================================================================
# PpssppLauncher._wait_for_port
# ============================================================================


class TestWaitForPort:
    """_wait_for_port: two-phase port discovery."""

    def test_proc_none_returns_false(self):
        launcher = PpssppLauncher(ws_port=12345)
        assert launcher._wait_for_port() is False

    def test_ws_port_none_returns_false(self):
        launcher = PpssppLauncher()
        launcher._proc = MagicMock(spec=subprocess.Popen)
        assert launcher._wait_for_port() is False

    def test_phase1_port_listening_returns_true(self):
        launcher = PpssppLauncher(ws_port=12345)
        launcher._proc = MagicMock(spec=subprocess.Popen)
        launcher._proc.poll.return_value = None
        with patch.object(launcher, "_is_port_listening", return_value=True):
            assert launcher._wait_for_port(timeout=2.0) is True

    def test_phase1_process_died_returns_false(self):
        launcher = PpssppLauncher(ws_port=12345)
        launcher._proc = MagicMock(spec=subprocess.Popen)
        launcher._proc.poll.return_value = 0  # exited
        # Phase 1 loop runs once: _is_port_listening=False, then poll()=0 → return False.
        with (
            patch.object(launcher, "_is_port_listening", return_value=False),
            patch(
                "ppsspp_dfx_mcp.core.launcher.time.time",
                side_effect=[0.0, 0.1, 0.2],  # deadline=1.0; loop enters once
            ),
            patch("ppsspp_dfx_mcp.core.launcher.time.sleep"),
        ):
            assert launcher._wait_for_port(timeout=2.0) is False

    def test_phase2_discover_updates_ws_port(self):
        launcher = PpssppLauncher(ws_port=12345)
        launcher._proc = MagicMock(spec=subprocess.Popen)
        launcher._proc.pid = 99999
        launcher._proc.poll.return_value = None
        # Phase 1 deadline expires immediately (time.time: 0.0 then 2.0 > 1.0),
        # so the while loop body never runs. Phase 2 discovers port 54321,
        # updates ws_port, then final _is_port_listening returns True.
        with (
            patch.object(launcher, "_is_port_listening", return_value=True),
            patch(
                "ppsspp_dfx_mcp.core.launcher.time.time",
                side_effect=[0.0, 2.0],
            ),
            patch("ppsspp_dfx_mcp.core.launcher.time.sleep"),
            patch(
                "ppsspp_dfx_mcp.core.launcher._discover_listening_port_for_pid",
                return_value=54321,
            ),
        ):
            assert launcher._wait_for_port(timeout=2.0) is True
        assert launcher.ws_port == 54321

    def test_phase2_discover_none_returns_false(self):
        launcher = PpssppLauncher(ws_port=12345)
        launcher._proc = MagicMock(spec=subprocess.Popen)
        launcher._proc.pid = 99999
        launcher._proc.poll.return_value = None
        # Phase 1 deadline expires immediately; Phase 2 discovers nothing.
        with (
            patch.object(launcher, "_is_port_listening", return_value=False),
            patch(
                "ppsspp_dfx_mcp.core.launcher.time.time",
                side_effect=[0.0, 2.0],
            ),
            patch("ppsspp_dfx_mcp.core.launcher.time.sleep"),
            patch(
                "ppsspp_dfx_mcp.core.launcher._discover_listening_port_for_pid",
                return_value=None,
            ),
        ):
            assert launcher._wait_for_port(timeout=2.0) is False

    def test_phase2_pid_zero_returns_false(self):
        launcher = PpssppLauncher(ws_port=12345)
        launcher._proc = MagicMock(spec=subprocess.Popen)
        launcher._proc.pid = 0
        launcher._proc.poll.return_value = None
        # Phase 1 deadline expires immediately; Phase 2 pid<=0 short-circuits.
        with (
            patch.object(launcher, "_is_port_listening", return_value=False),
            patch(
                "ppsspp_dfx_mcp.core.launcher.time.time",
                side_effect=[0.0, 2.0],
            ),
            patch("ppsspp_dfx_mcp.core.launcher.time.sleep"),
            patch("ppsspp_dfx_mcp.core.launcher._discover_listening_port_for_pid") as mock_discover,
        ):
            assert launcher._wait_for_port(timeout=2.0) is False
        mock_discover.assert_not_called()


# ============================================================================
# PpssppLauncher._is_port_listening
# ============================================================================


class TestIsPortListening:
    """_is_port_listening: socket connect_ex check."""

    def test_ws_port_none_returns_false(self):
        launcher = PpssppLauncher(ws_port=None)
        assert launcher._is_port_listening() is False

    def test_port_listening_returns_true(self):
        launcher = PpssppLauncher(ws_port=12345)
        fake_sock = MagicMock()
        fake_sock.connect_ex.return_value = 0
        with patch(
            "ppsspp_dfx_mcp.core.launcher.socket.socket",
            return_value=fake_sock,
        ):
            assert launcher._is_port_listening() is True

    def test_port_not_listening_returns_false(self):
        launcher = PpssppLauncher(ws_port=12345)
        fake_sock = MagicMock()
        fake_sock.connect_ex.return_value = 10061  # WSAECONNREFUSED
        with patch(
            "ppsspp_dfx_mcp.core.launcher.socket.socket",
            return_value=fake_sock,
        ):
            assert launcher._is_port_listening() is False

    def test_socket_exception_returns_false(self):
        launcher = PpssppLauncher(ws_port=12345)
        with patch(
            "ppsspp_dfx_mcp.core.launcher.socket.socket",
            side_effect=OSError("boom"),
        ):
            assert launcher._is_port_listening() is False


# ============================================================================
# PpssppLauncher.stop
# ============================================================================


class TestStop:
    """stop: terminate → kill → force-kill + cleanup + reset."""

    def test_no_proc_no_op(self):
        launcher = PpssppLauncher(ws_port=12345)
        launcher.stop()
        assert launcher._proc is None
        assert launcher.ws_port is None  # still reset

    def test_running_proc_terminate_success(self):
        launcher = PpssppLauncher(ws_port=12345)
        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.poll.return_value = None  # running
        launcher._proc = fake_proc
        launcher.stop()
        fake_proc.terminate.assert_called_once()
        fake_proc.wait.assert_called_once_with(timeout=3)
        assert launcher._proc is None

    def test_terminate_timeout_triggers_kill(self):
        launcher = PpssppLauncher(ws_port=12345)
        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.poll.return_value = None
        fake_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="x", timeout=3),
            None,  # kill().wait() succeeds
        ]
        launcher._proc = fake_proc
        launcher.stop()
        fake_proc.kill.assert_called_once()
        assert launcher._proc is None

    def test_kill_timeout_triggers_force_kill(self):
        launcher = PpssppLauncher(ws_port=12345)
        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.poll.return_value = None
        fake_proc.pid = 99999
        fake_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="x", timeout=3),
            subprocess.TimeoutExpired(cmd="x", timeout=2),
        ]
        launcher._proc = fake_proc
        with patch("ppsspp_dfx_mcp.core.launcher._force_kill_pid") as mock_force:
            launcher.stop()
        mock_force.assert_called_once_with(99999)
        assert launcher._proc is None

    def test_appendconfig_path_cleaned_up(self, tmp_path):
        launcher = PpssppLauncher(ws_port=12345)
        ini = tmp_path / "appended.ini"
        ini.write_text("[SystemParam]", encoding="utf-8")
        launcher._appendconfig_path = ini
        launcher.stop()
        assert not ini.exists()
        assert launcher._appendconfig_path is None

    def test_appendconfig_cleanup_ignores_oserror(self, tmp_path):
        launcher = PpssppLauncher(ws_port=12345)
        ini = tmp_path / "missing.ini"  # doesn't exist
        launcher._appendconfig_path = ini
        launcher.stop()
        # No raise; path cleared.
        assert launcher._appendconfig_path is None

    def test_ws_port_reset_to_none(self):
        launcher = PpssppLauncher(ws_port=12345)
        launcher._proc = None
        launcher.stop()
        assert launcher.ws_port is None

    def test_already_exited_proc_no_terminate(self):
        launcher = PpssppLauncher(ws_port=12345)
        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.poll.return_value = 0  # already exited
        launcher._proc = fake_proc
        launcher.stop()
        fake_proc.terminate.assert_not_called()
        assert launcher._proc is None


# ============================================================================
# Properties
# ============================================================================


class TestProperties:
    """is_running / pid / read_log properties."""

    def test_is_running_no_proc(self):
        launcher = PpssppLauncher()
        assert launcher.is_running is False

    def test_is_running_proc_running(self):
        launcher = PpssppLauncher()
        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.poll.return_value = None
        launcher._proc = fake_proc
        assert launcher.is_running is True

    def test_is_running_proc_exited(self):
        launcher = PpssppLauncher()
        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.poll.return_value = 0
        launcher._proc = fake_proc
        assert launcher.is_running is False

    def test_pid_no_proc(self):
        launcher = PpssppLauncher()
        assert launcher.pid is None

    def test_pid_with_proc(self):
        launcher = PpssppLauncher()
        fake_proc = MagicMock(spec=subprocess.Popen)
        fake_proc.pid = 12345
        launcher._proc = fake_proc
        assert launcher.pid == 12345

    def test_read_log_no_log_path(self):
        launcher = PpssppLauncher()
        assert launcher.read_log() == ""

    def test_read_log_file_not_exist(self, tmp_path):
        launcher = PpssppLauncher(log_path=tmp_path / "missing.log")
        assert launcher.read_log() == ""

    def test_read_log_returns_content(self, tmp_path):
        log = tmp_path / "ppsspp.log"
        log.write_text("log line 1\nlog line 2\n", encoding="utf-8")
        launcher = PpssppLauncher(log_path=log)
        assert launcher.read_log() == "log line 1\nlog line 2\n"


class TestWarnDebuggerExposedExternally:
    """C1 guard: the non-loopback bind warning path must not raise.

    The v1 🟡8 fix (loopback exposure detection) originally referenced an
    undefined `logger` name — F821 turned the security warning itself into
    a NameError that crashed `start()` after Popen had already succeeded
    (leaving an orphan PPSSPP). These tests lock the warning path so the
    guard can never regress into the crash it exists to prevent.
    """

    def _patch_binds(self, monkeypatch, binds):
        from ppsspp_dfx_mcp.core import launcher as launcher_mod

        monkeypatch.setattr(
            launcher_mod,
            "_get_listening_binds_for_pid_windows",
            lambda pid: binds,
        )

    def test_nonloopback_bind_logs_warning_without_raising(self, monkeypatch, caplog):
        """0.0.0.0 bind → WSDBG-EXPOSED warning, no exception."""
        import logging

        from ppsspp_dfx_mcp.core import launcher as launcher_mod

        self._patch_binds(monkeypatch, [(7777, "0.0.0.0")])
        with caplog.at_level(logging.WARNING):
            launcher_mod.warn_if_debugger_exposed_externally(4242, 7777)
        assert "WSDBG-EXPOSED" in caplog.text
        assert "RemoteDebuggerLocal" in caplog.text

    def test_loopback_bind_is_silent(self, monkeypatch, caplog):
        """127.0.0.1 bind → no warning (healthy configuration)."""
        import logging

        from ppsspp_dfx_mcp.core import launcher as launcher_mod

        self._patch_binds(monkeypatch, [(7777, "127.0.0.1")])
        with caplog.at_level(logging.WARNING):
            launcher_mod.warn_if_debugger_exposed_externally(4242, 7777)
        assert "WSDBG-EXPOSED" not in caplog.text

    def test_port_mismatch_is_silent(self, monkeypatch, caplog):
        """Debugger port different from the tracked one → no warning."""
        import logging

        from ppsspp_dfx_mcp.core import launcher as launcher_mod

        self._patch_binds(monkeypatch, [(9999, "0.0.0.0")])
        with caplog.at_level(logging.WARNING):
            launcher_mod.warn_if_debugger_exposed_externally(4242, 7777)
        assert "WSDBG-EXPOSED" not in caplog.text

    def test_nonpositive_pid_short_circuits(self, monkeypatch):
        """pid<=0 → no netstat call at all."""
        from ppsspp_dfx_mcp.core import launcher as launcher_mod

        calls = []
        monkeypatch.setattr(
            launcher_mod,
            "_get_listening_binds_for_pid_windows",
            lambda pid: calls.append(pid),
        )
        launcher_mod.warn_if_debugger_exposed_externally(0, 7777)
        assert calls == []
