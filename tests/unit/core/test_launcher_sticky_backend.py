"""PPSSPP's sticky GPU-failure list must be cleared before each launch (D16).

Measured 2026-10-02 on this host:

  FailedGraphicsBackends.txt == "VULKAN"
    -> ws_connected=true but a MODAL "图像错误" dialog (#32770) is up and
       gpu.stats never answers
  file absent
    -> start 1.6-2.1s, ws_connected=true, gpu.stats answers, no dialog

The file self-reinforces: NativeApp.cpp:454 reads it, Config.cpp:503
rotates off the backend, the rotation pops GRAPHICS_BACKEND_FAILED_ALERT
(Windows/main.cpp:784), and NativeApp.cpp:492 rewrites the file -- so it
survives every run. Only ClearFailedGPUBackends() (NativeApp.cpp:495),
reached after a successful graphics start, would remove it, and the
dialog makes that path unreachable.

  C6.7  a recorded failure list is deleted before the process spawns
  C6.8  "IGNORE" (PPSSPP's own opt-out) is NOT deleted -- deleting it
        would silently revoke a choice the operator made
  C6.9  absence is the normal case and must be a silent no-op
  C6.10 start() records what it cleared, so a caller can see it happened
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

import pytest

import ppsspp_dfx_mcp.core.launcher as _launcher_mod
from ppsspp_dfx_mcp.core.launcher import (
    PpssppLauncher,
    _clear_failed_gpu_backends,
)

REL = Path("memstick") / "PSP" / "SYSTEM" / "FailedGraphicsBackends.txt"


def _sys(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point Path.home() at tmp_path so the Documents candidate is contained."""
    fake = tmp_path / "home"
    fake.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    return fake


class TestClearsFailureList:
    """C6.7 / C6.9"""

    def test_deletes_recorded_failure(self, tmp_path: Path) -> None:
        target = tmp_path / REL
        target.parent.mkdir(parents=True)
        target.write_text("VULKAN", encoding="utf-8")
        cleared = _clear_failed_gpu_backends(tmp_path)
        assert not target.exists(), "the failure list survived; D16 will recur"
        assert cleared == target

    def test_absent_file_is_a_silent_noop(self, tmp_path: Path) -> None:
        assert _clear_failed_gpu_backends(tmp_path) is None

    def test_only_removes_it_not_the_ini(self, tmp_path: Path) -> None:
        """The ini holds the user's real settings; it must be untouched."""
        ini = tmp_path / REL.parent / "ppsspp.ini"
        ini.parent.mkdir(parents=True)
        ini.write_text("GraphicsBackend = 3 (VULKAN)\n", encoding="utf-8")
        (ini.parent / "FailedGraphicsBackends.txt").write_text("VULKAN", encoding="utf-8")
        _clear_failed_gpu_backends(tmp_path)
        assert ini.is_file(), "ppsspp.ini was deleted"
        assert "GraphicsBackend" in ini.read_text(encoding="utf-8")

    def test_does_not_raise_on_unwritable_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A locked file must not stop the launch."""
        target = tmp_path / REL
        target.parent.mkdir(parents=True)
        target.write_text("VULKAN", encoding="utf-8")
        real_unlink = Path.unlink

        def boom(self: Path, *a: object, **k: object) -> None:
            if self.name == "FailedGraphicsBackends.txt":
                raise PermissionError("locked")
            real_unlink(self, *a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "unlink", boom)
        # must not raise
        _clear_failed_gpu_backends(tmp_path)
        assert target.is_file(), (
            "the locked file must survive: swallowing the PermissionError must "
            "not remove or empty the file via another path"
        )
        assert target.read_text(encoding="utf-8") == "VULKAN"


class TestIgnoreIsRespected:
    """C6.8 -- "IGNORE" is the operator's documented opt-out."""

    def test_ignore_marker_survives(self, tmp_path: Path) -> None:
        target = tmp_path / REL
        target.parent.mkdir(parents=True)
        target.write_text("IGNORE", encoding="utf-8")
        cleared = _clear_failed_gpu_backends(tmp_path)
        assert target.is_file(), (
            "IGNORE was deleted; that is PPSSPP's opt-out for this whole "
            "mechanism (NativeApp.cpp:462) and revoking it silently would "
            "reintroduce the dialog"
        )
        assert cleared is None

    def test_ignore_with_surrounding_space(self, tmp_path: Path) -> None:
        target = tmp_path / REL
        target.parent.mkdir(parents=True)
        target.write_text("  IGNORE  \n", encoding="utf-8")
        _clear_failed_gpu_backends(tmp_path)
        assert target.is_file()


class TestUsersDocumentsLocation:
    """The non-portable location is cleared too."""

    def test_documents_copy_is_cleared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = _sys(monkeypatch, tmp_path)
        target = home / "Documents" / "PPSSPP" / "PSP" / "SYSTEM" / "FailedGraphicsBackends.txt"
        target.parent.mkdir(parents=True)
        target.write_text("VULKAN", encoding="utf-8")
        cleared = _clear_failed_gpu_backends(tmp_path / "nonexistent-exe-dir")
        assert not target.exists(), "the Documents copy was left behind"
        assert cleared == target


class TestStartWiring:
    """C6.10 -- start() must do it, and say what it did."""

    def test_attribute_defaults_to_none(self) -> None:
        launcher = PpssppLauncher(exe_path=Path("dummy.exe"))
        assert launcher.cleared_failed_backends is None

    @pytest.mark.asyncio
    async def test_start_clears_before_spawning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _sys(monkeypatch, tmp_path)
        target = tmp_path / REL
        target.parent.mkdir(parents=True)
        target.write_text("VULKAN", encoding="utf-8")
        iso = tmp_path / "d.iso"
        iso.write_bytes(b"x")
        if sys.platform == "win32":
            exe = tmp_path / "fake.bat"
            exe.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
        else:
            exe = tmp_path / "fake.sh"
            exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            exe.chmod(0o755)

        launcher = PpssppLauncher(exe_path=exe)
        await launcher.start(iso_path=iso, wait_seconds=0)
        launcher.stop()
        assert not target.exists(), "start() did not clear the failure list"
        assert launcher.cleared_failed_backends == target

    def test_popen_follows_the_clear(self) -> None:
        """Ordering matters: clearing after the spawn would be too late."""
        src = Path(_launcher_mod.__file__).read_text(encoding="utf-8")
        clear_at = src.index("self.cleared_failed_backends = _clear_failed_gpu_backends")
        spawn_at = src.index("proc = subprocess.Popen(")
        assert clear_at < spawn_at, "the failure list must be cleared BEFORE the process spawns"


class TestRealPpssppUnaffected:
    """The helper must tolerate the real exe's directory layout."""

    def test_nonexistent_dir_is_fine(self, tmp_path: Path) -> None:
        assert _clear_failed_gpu_backends(tmp_path / "no-such-dir") is None

    def test_does_not_touch_unrelated_files(self, tmp_path: Path) -> None:
        keep = tmp_path / "keep.txt"
        keep.write_text("x", encoding="utf-8")
        with contextlib.suppress(OSError):
            _clear_failed_gpu_backends(tmp_path)
        assert keep.is_file()
