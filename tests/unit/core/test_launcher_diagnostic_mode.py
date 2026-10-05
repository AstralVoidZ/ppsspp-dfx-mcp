"""D16: the launcher must be able to retain PPSSPP's own diagnostics.

Measured baseline before this change: `subprocess.Popen(..., stdout=DEVNULL,
stderr=DEVNULL)` meant every line PPSSPP emitted was discarded. The user
reports MCP-launched sessions pop a "graphics driver crash" dialog; with
output discarded there is no evidence to distinguish a backend failure
from a device-lost from silence.

These tests pin the opt-in contract:
  C6.1 default behaviour is UNCHANGED (still DEVNULL)
  C6.2 diagnostic_mode routes both streams into log_path
  C6.3 stop() closes the sink so the log is flushed
  C6.4 graphics_backend is written into the appendconfig ini only when set
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
from pathlib import Path

import pytest

import ppsspp_dfx_mcp.core.launcher as _launcher_mod
from ppsspp_dfx_mcp.core.launcher import (
    PpssppLauncher,
    _write_appendconfig_ini,
)

EXE = Path("dummy.exe")


class TestDefaultBehaviourUnchanged:
    """C6.1 -- the opt-in must not alter any existing caller."""

    def test_defaults_are_opt_out(self) -> None:
        launcher = PpssppLauncher(exe_path=EXE)
        assert launcher.diagnostic_mode is False, (
            "diagnostic_mode must default to False or every existing caller "
            "silently starts writing log files"
        )
        assert launcher.graphics_backend is None, (
            "graphics_backend must default to None so the appendconfig ini "
            "is byte-identical to before"
        )

    def test_no_log_sink_before_start(self) -> None:
        launcher = PpssppLauncher(exe_path=EXE)
        assert launcher._log_sink is None


class TestGraphicsBackendPinning:
    """C6.4 -- the ini gain a graphics section only when asked."""

    def test_backend_omitted_when_none(self, tmp_path: Path) -> None:
        ini = _write_appendconfig_ini(12345)
        try:
            text = ini.read_text(encoding="utf-8")
        finally:
            ini.unlink(missing_ok=True)
        assert "[Graphics]" not in text, (
            "an unpinned backend must not emit a Graphics section -- that "
            "would change PPSSPP's behaviour for every existing session"
        )
        assert "RemoteISOPort = 12345" in text

    @pytest.mark.parametrize("backend", [0, 1, 2, 3])
    def test_backend_written_when_pinned(self, backend: int) -> None:
        ini = _write_appendconfig_ini(12345, backend)
        try:
            text = ini.read_text(encoding="utf-8")
        finally:
            ini.unlink(missing_ok=True)
        assert f"GraphicsBackend = {backend}" in text, (
            f"backend {backend} was not written; pinning would silently fail"
        )
        # the debugger settings must survive alongside it
        assert "RemoteDebuggerOnStartup = True" in text


def _fake_ppsspp(tmp_path: Path, body: str) -> Path:
    """A stand-in exe that tolerates the launcher's real argv.

    The launcher always passes `--appendconfig=<path>` plus the ISO, so a
    bare `python fake.py` would exit on an unknown option and never emit
    anything. sys.executable ignores sys.argv entirely, so the flags are
    stripped here instead.
    """
    script = tmp_path / "fake_ppsspp.py"
    script.write_text(
        f"import sys\nsys.argv = [a for a in sys.argv if not a.startswith('--')]\n{body}\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        wrapper = tmp_path / "fake_ppsspp.bat"
        wrapper.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        # POSIX can neither exec a .bat nor rely on the exec bit being implicit.
        wrapper = tmp_path / "fake_ppsspp.sh"
        wrapper.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
        )
        wrapper.chmod(0o755)
    return wrapper


class TestDiagnosticCapture:
    """C6.2 / C6.3 -- evidence must actually land in the file."""

    @pytest.mark.asyncio
    async def test_diagnostic_mode_writes_ppsspp_output(self, tmp_path: Path) -> None:
        """A fake PPSSPP that prints must land in log_path."""
        import asyncio

        exe = _fake_ppsspp(
            tmp_path,
            "print('GRAPHICS CRASH: device lost')\n"
            "import sys; print('stderr line', file=sys.stderr)\n",
        )
        iso = tmp_path / "dummy.iso"
        iso.write_bytes(b"x")
        log = tmp_path / "out" / "ppsspp_diag.log"

        launcher = PpssppLauncher(
            exe_path=exe,
            log_path=log,
            diagnostic_mode=True,
        )
        proc = await launcher.start(iso_path=iso, wait_seconds=0)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(proc.wait, 10)
        launcher.stop()

        text = log.read_text(encoding="utf-8") if log.is_file() else ""
        assert "GRAPHICS CRASH" in text, f"diagnostic output was not captured; log={text[:200]!r}"
        # stderr folded in, so the file carries both streams
        assert "stderr line" in text, "stderr was not captured"

    @pytest.mark.asyncio
    async def test_default_mode_creates_no_log(self, tmp_path: Path) -> None:
        """C6.1 -- without the opt-in nothing is written."""
        import asyncio

        exe = _fake_ppsspp(tmp_path, "print('SHOULD NOT BE CAPTURED')\n")
        iso = tmp_path / "dummy2.iso"
        iso.write_bytes(b"x")
        log = tmp_path / "never_written.log"

        launcher = PpssppLauncher(
            exe_path=exe,
            log_path=log,
            diagnostic_mode=False,
        )
        proc = await launcher.start(iso_path=iso, wait_seconds=0)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(proc.wait, 10)
        launcher.stop()
        assert not log.exists(), (
            "log file was created without diagnostic_mode -- default behaviour changed"
        )
        assert launcher._log_sink is None, "sink left open after stop()"

    @pytest.mark.asyncio
    async def test_stop_closes_sink(self, tmp_path: Path) -> None:
        """C6.3 -- an unclosed sink can lose buffered output."""
        import asyncio

        iso = tmp_path / "dummy3.iso"
        iso.write_bytes(b"x")
        launcher = PpssppLauncher(
            exe_path=_fake_ppsspp(tmp_path, "print('sink flush check')\n"),
            log_path=tmp_path / "sink.log",
            diagnostic_mode=True,
        )
        proc = await launcher.start(iso_path=iso, wait_seconds=0)
        launcher.stop()
        assert launcher._log_sink is None, "stop() left the sink open"
        with contextlib.suppress(Exception):
            await asyncio.to_thread(proc.wait, 10)

    def test_popen_uses_devnull_by_default(self) -> None:
        """Static check: DEVNULL must remain the default sink."""
        src = Path(_launcher_mod.__file__).read_text(encoding="utf-8")
        assert "sink = subprocess.DEVNULL" in src
        assert "err_sink = subprocess.DEVNULL" in src
        assert subprocess.DEVNULL is not None  # sanity: constant exists
