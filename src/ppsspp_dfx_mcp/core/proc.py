"""Process-liveness probe (cross-platform best-effort).

Lives in core because both the transport layer (stepping timeouts) and
the error-translation layer need PID liveness, while the definition
formerly sat in session_manager — a layer those modules must not
depend on. Consumers call ``proc.is_pid_alive(...)`` via module
attribute access so tests that patch ``proc.is_pid_alive`` take
effect at every call site.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["is_pid_alive"]


def is_pid_alive(pid: int | None) -> bool:
    """Check if a PID is still running (cross-platform best-effort).

    Windows: OpenProcess + GetExitCodeProcess (exit_code == STILL_ACTIVE
    means the process is still running). POSIX: os.kill(pid, 0) plus a
    /proc/<pid>/status zombie check on Linux (os.kill reports zombies as
    alive). Non-Linux POSIX falls back to os.kill(pid, 0) and may report
    zombies as alive (best-effort).
    """
    if pid is None:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            STILL_ACTIVE = 259
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                # api_ok = Win32 API call succeeded (NOT process liveness).
                # Process is alive only if api_ok AND exit_code == STILL_ACTIVE.
                api_ok = bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)))
                return api_ok and exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except OSError:
            # Filesystem-level errors (rare for ctypes calls); narrow here.
            return False
        except Exception:
            # ctypes / Win32 calls can surface non-OSError exceptions
            # (e.g. PyWin32 wrappers); treat as "not alive" defensively.
            return False
    else:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        # os.kill(pid, 0) returns True for zombies on POSIX. On Linux we
        # can disambiguate via /proc/<pid>/status; non-Linux POSIX keeps
        # the os.kill result (best-effort, may report zombies as alive).
        proc_status = Path("/proc") / str(pid) / "status"
        if proc_status.exists():
            try:
                for line in proc_status.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("State:"):
                        # Sample: "State:\tZ (zombie)" — first token after
                        # ':' stripped is the state letter.
                        state_token = line.split(":", 1)[1].strip().split()
                        if state_token and state_token[0].startswith("Z"):
                            return False
                        break
            except OSError:
                pass
        return True
