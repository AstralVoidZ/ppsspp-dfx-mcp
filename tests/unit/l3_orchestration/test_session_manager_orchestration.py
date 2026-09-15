"""L3 orchestration tests: SessionManager (B.1 spec §7 O7 invariants I1-I38).

Anchors: B.1 orchestration spec §7 O7 SessionManager 38 invariants across
module-level constants, _load_sessions defensive parsing, _save_sessions
atomic write, is_pid_alive cross-platform logic, start/stop/touch/list/gc
methods, idle_gc_loop, and the get_session_manager singleton.

Complementary to L4 regression tests: L4 anchors specific violation fixes;
L3 anchors cross-method orchestration behavior under the real SessionManager
implementation (with stub launchers simulating PpssppLauncher start/stop).

Test strategy:
- Use the autouse `isolated_sessions_path` fixture from `tests/conftest.py`
  to redirect sessions.json to a tmp_path (no real ~/.ppsspp-dfx touched).
- Stub `PpssppLauncher` via `_StubLauncher` (records start/stop calls,
  controllable ws_port, optional start exception).
- For `is_pid_alive`, use `os.getpid()` (alive), `None` (always False),
  and `999999` (dead on any reasonable test runner).

B.1 spec invariants anchored here (analysis_ppsspp_dfx_orchestration_
spec_current_v1.md §7):
- O7-I1..I3: module constants (IDLE_GC_THRESHOLD_S, IDLE_GC_INTERVAL_S,
  _SESSION_FIELDS frozenset with 8 fields)
- O7-I4..I8: _load_sessions (missing → {}, malformed JSON → {}, non-dict
  root → {}, unknown fields dropped, TypeError on entry → continue)
- O7-I9: _parse_dt (datetime passthrough, ISO 8601 parse, fallback now)
- O7-I10..I12: _save_sessions (tmp + os.replace, mkdir parents, ISO 8601)
- O7-I13..I18: is_pid_alive (None, Windows OpenProcess, POSIX os.kill,
  Linux /proc zombie, non-Linux POSIX best-effort)
- O7-I19..I21: start_session (launcher.start exception → stop cleanup,
  save-before-register, ws_port fallback)
- O7-I22..I25: stop_session (lock, launcher vs PID, swallow exception,
  with_stopped return)
- O7-I26..I27: get_session_state (no lock, SessionExpired)
- O7-I28..I29: touch_session (lock, with_updated_activity)
- O7-I30: list_sessions (no lock, filter dead + idle)
- O7-I31..I34: gc_idle_sessions (lock, in-lock serial stop, launcher vs
  PID, returns stopped_ids)
- O7-I35..I37: idle_gc_loop (CancelledError break, Exception continue,
  sleep-before-GC)
- O7-I38: get_session_manager singleton (lazy construction)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest

from ppsspp_dfx_mcp.errors import IsoNotFound, SessionExpired, SessionNotFound
from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session import session_manager as sm
from ppsspp_dfx_mcp.core import proc


# ============================================================================
# _StubLauncher — test double for PpssppLauncher
# ============================================================================


class _StubLauncher:
    """Stub launcher that records start/stop calls without spawning processes.

    Configuration:
    - `ws_port_value`: value returned by the `ws_port` attribute after
      `start()`. Defaults to 12345.
    - `start_exception`: if set, `start()` raises this exception.
    - `stop_calls`: count of `stop()` invocations.
    - `start_calls`: count of `start()` invocations.
    - `proc_pid`: PID returned via the `proc.pid` attribute (default 99999).
    """

    def __init__(
        self,
        ws_port_value: Optional[int] = 12345,
        start_exception: Optional[Exception] = None,
        proc_pid: int = 99999,
    ) -> None:
        self.ws_port: Optional[int] = ws_port_value
        self._start_exception = start_exception
        self._proc_pid = proc_pid
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self, iso_path: Path) -> Any:
        self.start_calls += 1
        if self._start_exception is not None:
            raise self._start_exception
        # Return a stub proc with a .pid attribute.
        proc = type("_StubProc", (), {"pid": self._proc_pid})()
        return proc

    def stop(self) -> None:
        self.stop_calls += 1


def _make_session(
    session_id: str = "test-sess",
    iso_path: str = "/tmp/fake.iso",
    pid: Optional[int] = 999999,
    last_active_at: Optional[datetime] = None,
    created_at: Optional[datetime] = None,
    exec_count: int = 0,
) -> Session:
    """Build a Session with sensible defaults for tests."""
    now = datetime.now(timezone.utc)
    return Session(
        session_id=session_id,
        iso_path=iso_path,
        pid=pid,
        ws_url="ws://127.0.0.1:12345/debugger",
        created_at=created_at or now,
        last_active_at=last_active_at or now,
        exec_count=exec_count,
    )


def _seed_sessions_json(path: Path, sessions: dict[str, Session]) -> None:
    """Write sessions to the (isolated) sessions.json path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {sid: sm._session_to_dict(s) for sid, s in sessions.items()}
    path.write_text(json.dumps(payload), encoding="utf-8")


# ============================================================================
# O7-I1..I3: Module-level constants
# ============================================================================


class TestModuleConstants:
    """L3: module-level constants match spec values.

    B.1 O7 §7.1 invariants I1-I3: threshold/interval values and the
    _SESSION_FIELDS frozenset shape.
    """

    def test_I1_idle_gc_threshold_is_1800(self):
        """O7-I1: IDLE_GC_THRESHOLD_S == 1800 (30 minutes)."""
        assert sm.IDLE_GC_THRESHOLD_S == 1800

    def test_I2_idle_gc_interval_is_60(self):
        """O7-I2: IDLE_GC_INTERVAL_S == 60 (scan once per minute)."""
        assert sm.IDLE_GC_INTERVAL_S == 60

    def test_I3_session_fields_is_frozenset(self):
        """O7-I3: _SESSION_FIELDS is frozenset containing the 9 known fields.

        Spec says "8 fields" but the code includes `extra` as a 9th
        (free-form metadata dict). Test anchors the code reality.
        """
        assert isinstance(sm._SESSION_FIELDS, frozenset)
        assert sm._SESSION_FIELDS == frozenset({
            "session_id", "iso_path", "pid", "ws_url",
            "created_at", "last_active_at", "exec_count",
            "ws_connected", "extra",
        })
        assert len(sm._SESSION_FIELDS) == 9


# ============================================================================
# O7-I4..I8: _load_sessions defensive parsing
# ============================================================================


class TestLoadSessions:
    """L3: _load_sessions handles missing/malformed/non-dict JSON defensively.

    B.1 O7 §7.6 invariants I4-I8: missing file returns {}, malformed JSON
    returns {}, non-dict root returns {}, unknown fields silently dropped,
    per-entry TypeError silently continues.
    """

    def test_I4_missing_file_returns_empty_dict(self, isolated_sessions_path: Path):
        """O7-I4: _load_sessions returns {} when sessions.json is missing."""
        # isolated_sessions_path points to a non-existent file by default.
        assert not isolated_sessions_path.exists()
        result = sm._load_sessions()
        assert result == {}

    def test_I5_malformed_json_returns_empty_dict(
        self, isolated_sessions_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """O7-I5: _load_sessions logs warning + returns {} on JSON parse error."""
        isolated_sessions_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_sessions_path.write_text("{not valid json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            result = sm._load_sessions()
        assert result == {}
        assert any("malformed" in rec.message for rec in caplog.records)

    def test_I6_non_dict_root_returns_empty_dict(
        self, isolated_sessions_path: Path, caplog: pytest.LogCaptureFixture
    ):
        """O7-I6: _load_sessions logs warning + returns {} when root is not dict."""
        isolated_sessions_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_sessions_path.write_text("[1, 2, 3]", encoding="utf-8")
        with caplog.at_level("WARNING"):
            result = sm._load_sessions()
        assert result == {}
        assert any("not a dict" in rec.message for rec in caplog.records)

    def test_I7_unknown_fields_silently_dropped(self, isolated_sessions_path: Path):
        """O7-I7: _load_sessions filters to _SESSION_FIELDS, dropping unknown keys."""
        sess = _make_session()
        payload = {
            sess.session_id: {
                **sm._session_to_dict(sess),
                "launcher": "should-be-dropped",
                "future_field": "also-dropped",
            }
        }
        isolated_sessions_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_sessions_path.write_text(json.dumps(payload), encoding="utf-8")

        result = sm._load_sessions()
        assert sess.session_id in result
        loaded = result[sess.session_id]
        # Session was constructed without TypeError (unknown keys dropped).
        assert loaded.session_id == sess.session_id
        assert loaded.iso_path == sess.iso_path

    def test_I8_entry_type_error_silently_continues(self, isolated_sessions_path: Path):
        """O7-I8: _load_sessions skips entries that raise TypeError on Session(**).

        Trigger by giving a session entry wrong types for required fields
        (e.g. exec_count as a string that can't coerce).
        """
        good_sess = _make_session(session_id="good-sess")
        # Build a bad entry: pid is a non-int string. Session.__init__ does
        # not type-check, so we need a field that actually raises TypeError.
        # `extra` is typed as dict — passing a non-dict will raise TypeError
        # when dataclass frozen=True tries to hash/set it? Actually dataclass
        # __init__ does NOT type-check. We need to pass an unexpected argument
        # that causes TypeError. The _SESSION_FIELDS filter blocks unknown
        # keys, so we corrupt a known field that Session requires as a
        # specific type — but Session's only required positional is
        # session_id and iso_path. Trick: make created_at an int (non-datetime)
        # → with_updated_activity fails later, but __init__ succeeds.
        # The real TypeError source: _parse_dt handles any input. So we need
        # a different angle: pass `exec_count` as a string "abc" — dataclass
        # accepts. Hmm. The real path is passing unexpected keyword args,
        # but _SESSION_FIELDS filter blocks those.
        #
        # The actual production code path for I8: sess_dict is not a dict
        # (e.g. a list or string). That branch is guarded by
        # `isinstance(sess_dict, dict)` at L89. So I8's TypeError comes from
        # Session(**filtered) where filtered has unexpected type values that
        # the dataclass __init__ rejects. We force this by passing a session
        # entry that IS a dict but has `extra` as a string (not dict) —
        # dataclass will accept it (no runtime check). Or we patch Session
        # to raise TypeError. Cleanest: directly test the `continue` path
        # by making one entry's `session_id` missing (required positional
        # arg) — Session(**filtered) raises TypeError for missing required arg.
        bad_entry = {
            # Missing session_id and iso_path → TypeError on Session(**).
            "pid": 12345,
            "ws_url": "ws://x/debugger",
        }
        payload = {
            "bad-sess": bad_entry,
            good_sess.session_id: sm._session_to_dict(good_sess),
        }
        isolated_sessions_path.parent.mkdir(parents=True, exist_ok=True)
        isolated_sessions_path.write_text(json.dumps(payload), encoding="utf-8")

        result = sm._load_sessions()
        # Bad entry skipped; good entry loaded.
        assert "bad-sess" not in result
        assert good_sess.session_id in result


# ============================================================================
# O7-I9: _parse_dt
# ============================================================================


class TestParseDt:
    """L3: _parse_dt accepts datetime / ISO 8601, falls back to now(utc).

    B.1 O7 §7.6 invariant I9: datetime passthrough, ISO 8601 parse with
    tz-awareness, fallback to datetime.now(utc) on parse failure.
    """

    def test_I9a_datetime_passthrough(self):
        """O7-I9: _parse_dt returns datetime instances as-is."""
        dt = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
        assert sm._parse_dt(dt) is dt

    def test_I9b_iso8601_string_parsed(self):
        """O7-I9: _parse_dt parses ISO 8601 strings."""
        dt_str = "2026-07-22T12:00:00+00:00"
        result = sm._parse_dt(dt_str)
        assert result == datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
        assert result.tzinfo is not None

    def test_I9c_naive_iso_gets_utc_timezone(self):
        """O7-I9: _parse_dt attaches UTC tzinfo to naive ISO strings."""
        dt_str = "2026-07-22T12:00:00"
        result = sm._parse_dt(dt_str)
        assert result.tzinfo is timezone.utc

    def test_I9d_invalid_string_falls_back_to_now(self):
        """O7-I9: _parse_dt returns now(utc) on unparseable input."""
        before = datetime.now(timezone.utc)
        result = sm._parse_dt("not-a-date")
        after = datetime.now(timezone.utc)
        assert before <= result <= after
        assert result.tzinfo is timezone.utc

    def test_I9e_none_falls_back_to_now(self):
        """O7-I9: _parse_dt returns now(utc) on None input."""
        before = datetime.now(timezone.utc)
        result = sm._parse_dt(None)
        after = datetime.now(timezone.utc)
        assert before <= result <= after


# ============================================================================
# O7-I10..I12: _save_sessions atomic write + serialization
# ============================================================================


class TestSaveSessions:
    """L3: _save_sessions uses tmp + os.replace, creates parent dirs.

    B.1 O7 §7.8 invariants I10-I12: atomic write (tmp + os.replace),
    mkdir parents=True, datetime → ISO 8601 serialization.
    """

    def test_I10_uses_atomic_write_with_tmp_and_replace(
        self, isolated_sessions_path: Path
    ):
        """O7-I10: _save_sessions writes sessions.json.tmp then os.replace."""
        captured: dict[str, Path] = {}
        original_replace = os.replace

        def spy_replace(src, dst):
            captured["src"] = Path(src)
            captured["dst"] = Path(dst)
            original_replace(src, dst)

        with patch("ppsspp_dfx_mcp.session.session_manager.os.replace", spy_replace):
            sm._save_sessions({"x": _make_session(session_id="x")})

        expected_tmp = isolated_sessions_path.with_name(
            isolated_sessions_path.name + ".tmp"
        )
        assert captured["src"] == expected_tmp
        assert captured["dst"] == isolated_sessions_path
        # tmp file consumed by os.replace
        assert not expected_tmp.exists()
        # target file written
        assert isolated_sessions_path.exists()

    def test_I11_creates_parent_dirs(self, isolated_sessions_path: Path):
        """O7-I11: _save_sessions creates parent directories if missing."""
        # Move the sessions path to a deeply nested non-existent location.
        nested = isolated_sessions_path.parent / "deep" / "sub" / "sessions.json"

        def nested_path() -> Path:
            return nested

        with patch(
            "ppsspp_dfx_mcp.session.session_manager.sessions_path", nested_path
        ), patch("ppsspp_dfx_mcp.config.sessions_path", nested_path):
            sm._save_sessions({"x": _make_session(session_id="x")})

        assert nested.exists()
        assert nested.parent.is_dir()

    def test_I12_session_to_dict_serializes_datetimes_as_iso8601(self):
        """O7-I12: _session_to_dict emits ISO 8601 strings for datetime fields."""
        sess = Session(
            session_id="s1",
            iso_path="/tmp/x.iso",
            pid=123,
            created_at=datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc),
            last_active_at=datetime(2026, 7, 22, 12, 30, 0, tzinfo=timezone.utc),
        )
        d = sm._session_to_dict(sess)
        assert d["created_at"] == "2026-07-22T12:00:00+00:00"
        assert d["last_active_at"] == "2026-07-22T12:30:00+00:00"
        # Round-trip: load back via _parse_dt.
        assert sm._parse_dt(d["created_at"]) == sess.created_at
        assert sm._parse_dt(d["last_active_at"]) == sess.last_active_at


# ============================================================================
# O7-I13..I18: is_pid_alive cross-platform
# ============================================================================


class TestIsPidAlive:
    """L3: is_pid_alive handles None / current / dead PIDs cross-platform.

    B.1 O7 §7.9 invariants I13-I18: None → False, current process → True,
    dead PID → False. Windows uses OpenProcess+GetExitCodeProcess; POSIX
    uses os.kill(pid, 0) with Linux /proc zombie check.
    """

    def test_I13_none_returns_false(self):
        """O7-I13: is_pid_alive(None) returns False."""
        assert proc.is_pid_alive(None) is False

    def test_I14_current_process_returns_true(self):
        """O7-I14/I15/I16: the current process's PID is reported alive."""
        # On Windows: OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, pid)
        # returns a non-zero handle for the current process, and
        # GetExitCodeProcess returns STILL_ACTIVE (259).
        # On POSIX: os.kill(os.getpid(), 0) succeeds (no exception).
        assert proc.is_pid_alive(os.getpid()) is True

    def test_I15_dead_pid_returns_false(self):
        """O7-I15/I16: a PID with no live process returns False.

        999999 is chosen as an extremely high PID unlikely to collide
        with any real process on test runners.
        """
        dead_pid = 999999
        assert dead_pid != os.getpid()
        assert proc.is_pid_alive(dead_pid) is False

    def test_I17_windows_open_process_zero_returns_false(self):
        """O7-I15: Windows path returns False when OpenProcess returns 0.

        Tested by patching sys.platform to 'win32' and ctypes.windll.kernel32
        to return 0 from OpenProcess.
        """
        if sys.platform != "win32":
            # Simulate Windows path: patch sys.platform + inject fake kernel32.
            self._simulate_windows_open_process_zero()
        else:
            # On real Windows, OpenProcess(0x1000, False, 999999) returns 0
            # for a non-existent PID. We already verified this in test_I15.
            assert proc.is_pid_alive(999999) is False

    def _simulate_windows_open_process_zero(self) -> None:
        """Helper: simulate the Windows OpenProcess-returns-0 path on non-Windows."""
        import ctypes

        class _FakeKernel32:
            def OpenProcess(self, access, inherit, pid):
                return 0  # no handle → is_pid_alive returns False

            def GetExitCodeProcess(self, handle, byref):
                return False

            def CloseHandle(self, handle):
                return True

        with patch("sys.platform", "win32"), patch(
            "ctypes.windll", type("Windll", (), {"kernel32": _FakeKernel32()})
        ):
            assert proc.is_pid_alive(999999) is False

    def test_I18_posix_os_kill_exception_returns_false(self):
        """O7-I16: POSIX path returns False when os.kill raises OSError."""
        if sys.platform == "win32":
            pytest.skip("POSIX-only test")
        with patch("os.kill", side_effect=ProcessLookupError("no such process")):
            assert proc.is_pid_alive(999999) is False

    def test_I19_linux_zombie_state_returns_false(self):
        """O7-I17: Linux path reads /proc/<pid>/status; State: Z → False.

        Simulated by patching sys.platform to 'linux' and Path.exists/read_text
        to return a zombie status line.
        """
        # Force into the POSIX branch first (skip win32).
        if sys.platform == "win32":
            with patch("sys.platform", "linux"):
                self._simulate_linux_zombie()
        else:
            self._simulate_linux_zombie()

    def _simulate_linux_zombie(self) -> None:
        """Helper: simulate Linux /proc zombie detection."""
        # os.kill(pid, 0) succeeds (process entry exists).
        with patch("os.kill", lambda *a, **kw: None), patch(
            "pathlib.Path.exists", return_value=True
        ), patch(
            "pathlib.Path.read_text",
            return_value="Name:\tzombie\nState:\tZ (zombie)\n",
        ):
            assert proc.is_pid_alive(999999) is False

    def test_I20_posix_non_linux_best_effort_reports_alive(self):
        """O7-I18: POSIX non-Linux (no /proc) best-effort: os.kill ok → True.

        On macOS/BSD, /proc/<pid>/status doesn't exist, so the code falls
        through to `return True` after os.kill(pid, 0) succeeds. Zombies
        may be reported as alive (documented limitation).
        """
        # Force POSIX non-Linux path: os.kill succeeds, /proc absent.
        with patch("sys.platform", "darwin"), patch(
            "os.kill", lambda *a, **kw: None
        ), patch("pathlib.Path.exists", return_value=False):
            assert proc.is_pid_alive(999999) is True


# ============================================================================
# O7-I19..I21: start_session
# ============================================================================


class TestStartSession:
    """L3: start_session orchestration — launcher cleanup, save-before-register.

    B.1 O7 §7.4 invariants I19-I21: launcher.start exception triggers
    launcher.stop cleanup; _launchers registered only after _save_sessions;
    ws_port fallback to config when launcher.ws_port is None.
    """

    async def test_I19_launcher_start_exception_triggers_cleanup(
        self, isolated_sessions_path: Path, tmp_path: Path
    ):
        """O7-I19: launcher.start exception → await launcher.stop() + raise."""
        # Create a real ISO file so the IsoNotFound check passes.
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00" * 16)

        stub = _StubLauncher(start_exception=RuntimeError("boom"))
        with patch("ppsspp_dfx_mcp.session.session_manager.PpssppLauncher", lambda: stub):
            manager = sm.SessionManager()
            with pytest.raises(RuntimeError, match="boom"):
                await manager.start_session(str(iso))

        # stop() was called for cleanup.
        assert stub.stop_calls == 1
        # Session was not persisted (start failed before save).
        assert not isolated_sessions_path.exists()
        # Launcher was NOT registered in _launchers.
        assert manager._launchers == {}

    async def test_I20_save_before_register_launcher(
        self, isolated_sessions_path: Path, tmp_path: Path
    ):
        """O7-I20: _save_sessions runs BEFORE _launchers[session_id] = launcher.

        Verified by making _save_sessions raise: the launcher must NOT be
        in _launchers afterwards (no leak).
        """
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00" * 16)

        stub = _StubLauncher(ws_port_value=12345)
        manager = sm.SessionManager()

        # Patch _save_sessions to raise on the first call.
        with patch(
            "ppsspp_dfx_mcp.session.session_manager.PpssppLauncher", lambda: stub
        ), patch(
            "ppsspp_dfx_mcp.session.session_manager._save_sessions",
            side_effect=OSError("disk full"),
        ):
            with pytest.raises(OSError, match="disk full"):
                await manager.start_session(str(iso))

        # Launcher NOT in _launchers (save failed → no registration).
        assert manager._launchers == {}
        # Launcher's start() was called (and succeeded, before save).
        assert stub.start_calls == 1

    async def test_I21_ws_port_fallback_when_launcher_ws_port_none(
        self, isolated_sessions_path: Path, tmp_path: Path
    ):
        """O7-I21: launcher.ws_port is None → fallback to config ws_port()."""
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00" * 16)

        stub = _StubLauncher(ws_port_value=None)
        with patch("ppsspp_dfx_mcp.session.session_manager.PpssppLauncher", lambda: stub), patch(
            "ppsspp_dfx_mcp.session.session_manager.ws_port", return_value=23456
        ), patch(
            "ppsspp_dfx_mcp.session.session_manager._probe_ws_connection",
            return_value=False,
        ):
            manager = sm.SessionManager()
            sess = await manager.start_session(str(iso))

        # ws_url uses the fallback port.
        assert ":23456/" in sess.ws_url
        # Launcher registered after successful save.
        assert len(manager._launchers) == 1

    async def test_start_session_raises_iso_not_found(self, tmp_path: Path):
        """start_session raises IsoNotFound for non-existent ISO."""
        manager = sm.SessionManager()
        with pytest.raises(IsoNotFound):
            await manager.start_session(str(tmp_path / "missing.iso"))


# ============================================================================
# N-07 (方案 A): start_session probes WS connection after launch
# ============================================================================


class TestStartSessionProbeWS:
    """L3: start_session proactively probes WS liveness (N-07 方案 A).

    Pre-N-07, ``ws_connected`` stayed False until the first tool call
    opened a WS — confusing for callers that query session(get)
    immediately after ``start_session``. The probe runs the real
    handshake (WsTransport.connect + send_version) so the returned
    session reflects the true WS state.

    Invariants anchored here:
    - probe returns True → ws_connected=True persisted to sessions.json
    - probe returns False → ws_connected stays False (no exception, no
      state mutation beyond the default)
    - probe raises → start_session MUST NOT propagate (best-effort)
    """

    @staticmethod
    def _make_iso(tmp_path: Path) -> Path:
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00" * 16)
        return iso

    async def test_probe_true_persists_ws_connected_true(
        self, isolated_sessions_path: Path, tmp_path: Path
    ):
        """Probe succeeds → ws_connected=True persisted to disk."""
        iso = self._make_iso(tmp_path)
        stub = _StubLauncher(ws_port_value=12345)

        async def _probe_true(ws_url: str) -> bool:
            return True

        with patch(
            "ppsspp_dfx_mcp.session.session_manager.PpssppLauncher", lambda: stub
        ), patch(
            "ppsspp_dfx_mcp.session.session_manager._probe_ws_connection",
            _probe_true,
        ):
            manager = sm.SessionManager()
            sess = await manager.start_session(str(iso))

        # Returned session reflects the probe result.
        assert sess.ws_connected is True
        # Persistence: sessions.json has ws_connected=True for this sid.
        data = json.loads(isolated_sessions_path.read_text(encoding="utf-8"))
        assert data[sess.session_id]["ws_connected"] is True

    async def test_probe_false_keeps_ws_connected_false(
        self, isolated_sessions_path: Path, tmp_path: Path
    ):
        """Probe fails → ws_connected stays False (default, no exception)."""
        iso = self._make_iso(tmp_path)
        stub = _StubLauncher(ws_port_value=12345)

        async def _probe_false(ws_url: str) -> bool:
            return False

        with patch(
            "ppsspp_dfx_mcp.session.session_manager.PpssppLauncher", lambda: stub
        ), patch(
            "ppsspp_dfx_mcp.session.session_manager._probe_ws_connection",
            _probe_false,
        ):
            manager = sm.SessionManager()
            sess = await manager.start_session(str(iso))

        # ws_connected stays False (default) — no exception raised.
        assert sess.ws_connected is False
        data = json.loads(isolated_sessions_path.read_text(encoding="utf-8"))
        assert data[sess.session_id]["ws_connected"] is False

    async def test_probe_exception_does_not_propagate(
        self, isolated_sessions_path: Path, tmp_path: Path
    ):
        """Probe raises → start_session MUST NOT propagate (best-effort).

        The probe is run after the session is already persisted; a
        transient PPSSPP cold-start delay or unexpected probe failure
        MUST NOT break start_session. ws_connected stays False.
        """
        iso = self._make_iso(tmp_path)
        stub = _StubLauncher(ws_port_value=12345)

        async def _probe_raises(ws_url: str) -> bool:
            raise RuntimeError("unexpected probe failure")

        with patch(
            "ppsspp_dfx_mcp.session.session_manager.PpssppLauncher", lambda: stub
        ), patch(
            "ppsspp_dfx_mcp.session.session_manager._probe_ws_connection",
            _probe_raises,
        ):
            manager = sm.SessionManager()
            try:
                sess = await manager.start_session(str(iso))
            except Exception as e:
                pytest.fail(
                    f"N-07 contract violated: start_session propagated "
                    f"probe exception {e!r}; probe must be best-effort."
                )

        # Session still persisted with ws_connected=False.
        assert sess.ws_connected is False


# ============================================================================
# N-07 (方案 A): _probe_ws_connection direct unit tests
# ============================================================================


class TestProbeWsConnection:
    """L3: _probe_ws_connection returns True on handshake success, False otherwise.

    The probe is a thin wrapper over WsTransport.connect + send_version
    + close. Tests stub WsTransport to verify the dispatch logic
    without spawning a real WebSocket server.
    """

    async def test_probe_returns_true_on_successful_handshake(self):
        """Probe returns True when connect + send_version succeed."""
        from ppsspp_dfx_mcp.session.session_manager import _probe_ws_connection

        class _FakeTransport:
            def __init__(self) -> None:
                self.closed = False

            async def connect(self) -> None:
                pass

            async def send_version(self) -> dict:
                return {"name": "PPSSPP", "version": "1.0"}

            async def close(self) -> None:
                self.closed = True

        fake = _FakeTransport()
        with patch(
            "ppsspp_dfx_mcp.core.transport.WsTransport", lambda *a, **kw: fake
        ):
            # patchpath targets the WsTransport import inside the
            # _probe_ws_connection function (lazy import).
            result = await _probe_ws_connection("ws://127.0.0.1:12345/debugger")

        assert result is True
        assert fake.closed is True  # transport always closed.

    async def test_probe_returns_false_on_connect_failure(self):
        """Probe returns False when connect() raises (PPSSPP not ready)."""
        from ppsspp_dfx_mcp.session.session_manager import _probe_ws_connection

        class _FakeTransport:
            def __init__(self) -> None:
                self.closed = False

            async def connect(self) -> None:
                raise ConnectionRefusedError("PPSSPP not listening")

            async def send_version(self) -> dict:
                raise AssertionError("send_version should not be called")

            async def close(self) -> None:
                self.closed = True

        fake = _FakeTransport()
        with patch(
            "ppsspp_dfx_mcp.core.transport.WsTransport", lambda *a, **kw: fake
        ):
            result = await _probe_ws_connection("ws://127.0.0.1:12345/debugger")

        assert result is False
        assert fake.closed is True  # close still runs in finally.

    async def test_probe_returns_false_on_malformed_url(self):
        """Probe returns False on unparseable ws_url (no exception)."""
        from ppsspp_dfx_mcp.session.session_manager import _probe_ws_connection

        # urlparse is lenient — most "weird" inputs still parse. A URL
        # missing the host entirely is one of the few that yields
        # port=None (defaults to 12345 in the implementation). To truly
        # exercise the malformed path, we pass a non-string type that
        # urlparse would AttributeError on.
        result = await _probe_ws_connection(None)  # type: ignore[arg-type]
        assert result is False

    async def test_probe_returns_false_on_send_version_failure(self):
        """Probe returns False when send_version raises (handshake failed)."""
        from ppsspp_dfx_mcp.session.session_manager import _probe_ws_connection

        class _FakeTransport:
            def __init__(self) -> None:
                self.closed = False

            async def connect(self) -> None:
                pass

            async def send_version(self) -> dict:
                raise RuntimeError("subprotocol negotiation failed")

            async def close(self) -> None:
                self.closed = True

        fake = _FakeTransport()
        with patch(
            "ppsspp_dfx_mcp.core.transport.WsTransport", lambda *a, **kw: fake
        ):
            result = await _probe_ws_connection("ws://127.0.0.1:12345/debugger")

        assert result is False
        assert fake.closed is True


# ============================================================================
# O7-I22..I25: stop_session
# ============================================================================


class TestStopSession:
    """L3: stop_session orchestration — lock, launcher vs PID, swallow, with_stopped.

    B.1 O7 §7.5 invariants I22-I25: load-modify-save under _lock; in-memory
    launcher preferred over _force_kill_pid; _force_kill_pid exceptions
    swallowed; returns sess.with_stopped() and pops from sessions dict.
    """

    async def test_I22_stop_session_acquires_lock(
        self, isolated_sessions_path: Path
    ):
        """O7-I22: stop_session runs load-modify-save under self._lock.

        Three-phase locking (B.2 refactor): stop_session acquires the
        lock TWICE — once in Phase 1 (read session + pop launcher) and
        once in Phase 3 (remove session + persist). The middle phase
        (launcher.stop / _force_kill_pid) runs OUTSIDE the lock to
        avoid blocking other sessions during the slow stop call.
        """
        sess = _make_session(session_id="s1")
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()

        # Track lock acquisition.
        original_acquire = manager._lock.acquire
        original_release = manager._lock.release
        acquire_calls = {"n": 0}

        async def tracked_acquire():
            await original_acquire()
            acquire_calls["n"] += 1

        def tracked_release():
            original_release()

        manager._lock.acquire = tracked_acquire
        manager._lock.release = tracked_release
        try:
            await manager.stop_session("s1")
        finally:
            manager._lock.acquire = original_acquire
            manager._lock.release = original_release

        # Phase 1 (read session) + Phase 3 (persist removal) = 2 acquisitions.
        assert acquire_calls["n"] == 2

    async def test_I23a_prefers_in_memory_launcher(
        self, isolated_sessions_path: Path
    ):
        """O7-I23: stop_session uses in-memory launcher when available."""
        sess = _make_session(session_id="s1")
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()
        stub = _StubLauncher()
        manager._launchers["s1"] = stub

        await manager.stop_session("s1")

        # Launcher.stop was called.
        assert stub.stop_calls == 1
        # Launcher popped from _launchers.
        assert "s1" not in manager._launchers

    async def test_I23b_falls_back_to_force_kill_pid(
        self, isolated_sessions_path: Path
    ):
        """O7-I23: no in-memory launcher → _force_kill_pid(sess.pid)."""
        sess = _make_session(session_id="s1", pid=4321)
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()

        kill_calls: list[int] = []

        def fake_kill(pid: int) -> None:
            kill_calls.append(pid)

        with patch(
            "ppsspp_dfx_mcp.session.session_manager._force_kill_pid", fake_kill
        ):
            await manager.stop_session("s1")

        assert kill_calls == [4321]

    async def test_I24_force_kill_pid_exception_swallowed(
        self, isolated_sessions_path: Path
    ):
        """O7-I24: _force_kill_pid exception → silently passed (no raise)."""
        sess = _make_session(session_id="s1", pid=4321)
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()

        with patch(
            "ppsspp_dfx_mcp.session.session_manager._force_kill_pid",
            side_effect=OSError("permission denied"),
        ):
            # Must NOT raise.
            result = await manager.stop_session("s1")

        # Session still popped + with_stopped() returned.
        assert result.session_id == "s1"
        assert result.pid is None

    async def test_I25_returns_with_stopped_and_pops_session(
        self, isolated_sessions_path: Path
    ):
        """O7-I25: stop_session returns sess.with_stopped(); session removed from disk."""
        sess = _make_session(session_id="s1", pid=12345)
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()

        result = await manager.stop_session("s1")

        # with_stopped(): pid=None, ws_connected=False.
        assert result.session_id == "s1"
        assert result.pid is None
        assert result.ws_connected is False
        # Session removed from sessions.json.
        data = json.loads(isolated_sessions_path.read_text(encoding="utf-8"))
        assert "s1" not in data

    async def test_stop_session_raises_session_not_found(
        self, isolated_sessions_path: Path
    ):
        """stop_session raises SessionNotFound for unknown session_id."""
        manager = sm.SessionManager()
        with pytest.raises(SessionNotFound):
            await manager.stop_session("nonexistent-id")


# ============================================================================
# O7-I26..I27: get_session_state
# ============================================================================


class TestGetSessionState:
    """L3: get_session_state is a pure async read (no lock, no persist).

    B.1 O7 §7.7 invariants I26-I27: no lock acquired; raises SessionExpired
    when pid is set but not alive. The async refactor (B.2) moved file
    I/O to ``asyncio.to_thread`` via ``_load_sessions_async``, so the
    method is now ``async def`` and callers must ``await`` it. The
    ``self._lock`` is still NOT acquired — the read is lock-free.
    """

    async def test_I26_does_not_acquire_lock(self, isolated_sessions_path: Path):
        """O7-I26: get_session_state does not touch self._lock."""
        sess = _make_session(session_id="s1", pid=None)  # pid=None → no alive check
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()

        # Replace lock with one that raises if acquired.
        class _NoAcquireLock:
            def acquire(self): raise AssertionError("lock acquired")
            def release(self): pass
            async def __aenter__(self): raise AssertionError("lock acquired")
            async def __aexit__(self, *a): pass

        manager._lock = _NoAcquireLock()  # type: ignore[assignment]
        # Must NOT raise (no lock acquisition).
        result = await manager.get_session_state("s1")
        assert result.session_id == "s1"

    async def test_I27_raises_session_expired_when_pid_dead(
        self, isolated_sessions_path: Path
    ):
        """O7-I27: pid set + not alive → SessionExpired."""
        sess = _make_session(session_id="s1", pid=999999)
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()

        with pytest.raises(SessionExpired):
            await manager.get_session_state("s1")

    async def test_I27_skips_expired_check_when_pid_none(self, isolated_sessions_path: Path):
        """O7-I27: pid=None → no SessionExpired check (returns session)."""
        sess = _make_session(session_id="s1", pid=None)
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()
        result = await manager.get_session_state("s1")
        assert result.session_id == "s1"

    async def test_raises_session_not_found_for_unknown(self, isolated_sessions_path: Path):
        """get_session_state raises SessionNotFound for unknown session_id."""
        manager = sm.SessionManager()
        with pytest.raises(SessionNotFound):
            await manager.get_session_state("nope")


# ============================================================================
# O7-I28..I29: touch_session
# ============================================================================


class TestTouchSession:
    """L3: touch_session updates last_active_at + exec_count under lock.

    B.1 O7 §7.7 invariants I28-I29: load-modify-save under _lock;
    uses Session.with_updated_activity() to bump last_active_at + exec_count.
    """

    async def test_I28_runs_under_lock(self, isolated_sessions_path: Path):
        """O7-I28: touch_session acquires _lock for load-modify-save."""
        old = datetime.now(timezone.utc) - timedelta(seconds=100)
        sess = _make_session(session_id="s1", last_active_at=old, created_at=old)
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()

        original_acquire = manager._lock.acquire
        acquire_calls = {"n": 0}

        async def tracked_acquire():
            await original_acquire()
            acquire_calls["n"] += 1

        manager._lock.acquire = tracked_acquire
        try:
            await manager.touch_session("s1")
        finally:
            manager._lock.acquire = original_acquire

        assert acquire_calls["n"] == 1

    async def test_I29_bumps_last_active_at_and_exec_count(
        self, isolated_sessions_path: Path
    ):
        """O7-I29: touch_session uses with_updated_activity (last_active_at + exec_count)."""
        old = datetime.now(timezone.utc) - timedelta(seconds=100)
        sess = _make_session(
            session_id="s1", exec_count=5, last_active_at=old, created_at=old
        )
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()

        result = await manager.touch_session("s1")

        assert result.exec_count == 6
        assert result.last_active_at > old
        # Persisted to disk.
        data = json.loads(isolated_sessions_path.read_text(encoding="utf-8"))
        assert data["s1"]["exec_count"] == 6

    async def test_raises_session_not_found(self, isolated_sessions_path: Path):
        """touch_session raises SessionNotFound for unknown session_id."""
        manager = sm.SessionManager()
        with pytest.raises(SessionNotFound):
            await manager.touch_session("nope")


# ============================================================================
# O7-I30: list_sessions
# ============================================================================


class TestListSessions:
    """L3: list_sessions is a pure read filtering dead PIDs + idle sessions.

    B.1 O7 §7.7 invariant I30: no lock acquired; filters out sessions with
    dead PIDs and sessions with idle_s > IDLE_GC_THRESHOLD_S.

    Note: list_sessions is now async (uses _load_sessions_async to avoid
    blocking the event loop on file I/O).
    """

    @pytest.mark.asyncio
    async def test_I30a_does_not_acquire_lock(self, isolated_sessions_path: Path):
        """O7-I30: list_sessions does not acquire _lock."""
        sess = _make_session(session_id="s1", pid=None)
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()

        class _NoAcquireLock:
            def acquire(self): raise AssertionError("lock acquired")
            def release(self): pass
            async def __aenter__(self): raise AssertionError("lock acquired")
            async def __aexit__(self, *a): pass

        manager._lock = _NoAcquireLock()  # type: ignore[assignment]
        result = await manager.list_sessions()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_I30b_filters_dead_pid(self, isolated_sessions_path: Path):
        """O7-I30: sessions with dead PIDs are excluded."""
        alive_sess = _make_session(session_id="alive", pid=None)
        dead_sess = _make_session(session_id="dead", pid=999999)
        _seed_sessions_json(
            isolated_sessions_path, {"alive": alive_sess, "dead": dead_sess}
        )
        manager = sm.SessionManager()

        result = await manager.list_sessions()
        ids = [s.session_id for s in result]
        assert "alive" in ids
        assert "dead" not in ids

    @pytest.mark.asyncio
    async def test_I30c_filters_idle_expired(self, isolated_sessions_path: Path):
        """O7-I30: sessions with idle_s > IDLE_GC_THRESHOLD_S are excluded."""
        recent = _make_session(session_id="recent", pid=None)
        old_time = datetime.now(timezone.utc) - timedelta(seconds=3600)
        old = _make_session(session_id="old", pid=None, last_active_at=old_time, created_at=old_time)
        _seed_sessions_json(
            isolated_sessions_path, {"recent": recent, "old": old}
        )
        manager = sm.SessionManager()

        result = await manager.list_sessions()
        ids = [s.session_id for s in result]
        assert "recent" in ids
        assert "old" not in ids


# ============================================================================
# O7-I31..I34: gc_idle_sessions
# ============================================================================


class TestGcIdleSessions:
    """L3: gc_idle_sessions stops idle/dead sessions with three-phase locking.

    B.1 O7 §7.6 invariants (revised after F-06 fix): three-phase lock
    protocol mirrors stop_session — Phase 1 brief lock to scan + pop
    launchers, Phase 2 stop OUTSIDE the lock (launcher.stop can take
    5+ seconds), Phase 3 brief lock to persist. Expired sessions are
    stopped OUTSIDE the lock so concurrent session operations are not
    blocked; prefers in-memory launcher, else _force_kill_pid; returns
    list of stopped session_ids.
    """

    async def test_I31_acquires_lock_in_two_phases(self, isolated_sessions_path: Path):
        """O7-I31: gc_idle_sessions acquires _lock twice (Phase 1 + Phase 3)."""
        old_time = datetime.now(timezone.utc) - timedelta(seconds=3600)
        sess = _make_session(session_id="s1", pid=None, last_active_at=old_time, created_at=old_time)
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()

        original_acquire = manager._lock.acquire
        acquire_calls = {"n": 0}

        async def tracked_acquire():
            await original_acquire()
            acquire_calls["n"] += 1

        manager._lock.acquire = tracked_acquire
        try:
            await manager.gc_idle_sessions()
        finally:
            manager._lock.acquire = original_acquire

        # Two phases: scan (Phase 1) + persist (Phase 3).
        assert acquire_calls["n"] == 2

    async def test_I32_stops_outside_lock(self, isolated_sessions_path: Path):
        """O7-I32 (revised): launcher.stop / _force_kill_pid happen OUTSIDE the lock.

        F-06 fix: holding the lock during launcher.stop blocks all
        concurrent session operations for 5+ seconds per hung process.
        The three-phase protocol runs stop in Phase 2 (no lock held).
        """
        old_time = datetime.now(timezone.utc) - timedelta(seconds=3600)
        s1 = _make_session(session_id="s1", pid=4321, last_active_at=old_time, created_at=old_time)
        _seed_sessions_json(isolated_sessions_path, {"s1": s1})
        manager = sm.SessionManager()

        # Track whether lock is held during _force_kill_pid calls.
        in_lock_state: list[bool] = []

        def fake_kill(pid: int) -> None:
            in_lock_state.append(manager._lock.locked())

        with patch(
            "ppsspp_dfx_mcp.session.session_manager._force_kill_pid", fake_kill
        ):
            stopped = await manager.gc_idle_sessions()

        assert stopped == ["s1"]
        # _force_kill_pid happened while lock was NOT held (Phase 2).
        assert not any(in_lock_state), (
            f"expected all stops outside lock, got {in_lock_state}"
        )

    async def test_I33a_prefers_in_memory_launcher(self, isolated_sessions_path: Path):
        """O7-I33: gc_idle_sessions pops _launchers[sid] when available."""
        old_time = datetime.now(timezone.utc) - timedelta(seconds=3600)
        sess = _make_session(session_id="s1", pid=None, last_active_at=old_time, created_at=old_time)
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()
        stub = _StubLauncher()
        manager._launchers["s1"] = stub

        await manager.gc_idle_sessions()

        assert stub.stop_calls == 1
        assert "s1" not in manager._launchers

    async def test_I33b_falls_back_to_force_kill_pid(self, isolated_sessions_path: Path):
        """O7-I33: no in-memory launcher → _force_kill_pid(sess.pid)."""
        old_time = datetime.now(timezone.utc) - timedelta(seconds=3600)
        sess = _make_session(
            session_id="s1", pid=4321, last_active_at=old_time, created_at=old_time
        )
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()

        kill_calls: list[int] = []

        def fake_kill(pid: int) -> None:
            kill_calls.append(pid)

        with patch(
            "ppsspp_dfx_mcp.session.session_manager._force_kill_pid", fake_kill
        ):
            stopped = await manager.gc_idle_sessions()

        assert stopped == ["s1"]
        assert kill_calls == [4321]

    async def test_I34_returns_stopped_ids_list(self, isolated_sessions_path: Path):
        """O7-I34: gc_idle_sessions returns list[str] of stopped session_ids."""
        old_time = datetime.now(timezone.utc) - timedelta(seconds=3600)
        s1 = _make_session(session_id="s1", pid=None, last_active_at=old_time, created_at=old_time)
        s2 = _make_session(session_id="s2", pid=None, last_active_at=old_time, created_at=old_time)
        _seed_sessions_json(isolated_sessions_path, {"s1": s1, "s2": s2})
        manager = sm.SessionManager()

        result = await manager.gc_idle_sessions()

        assert isinstance(result, list)
        assert all(isinstance(x, str) for x in result)
        assert set(result) == {"s1", "s2"}

    async def test_returns_empty_when_no_expired(self, isolated_sessions_path: Path):
        """gc_idle_sessions returns [] when no sessions are expired."""
        sess = _make_session(session_id="s1", pid=None)  # active
        _seed_sessions_json(isolated_sessions_path, {"s1": sess})
        manager = sm.SessionManager()

        result = await manager.gc_idle_sessions()
        assert result == []

    async def test_persists_after_gc(self, isolated_sessions_path: Path):
        """gc_idle_sessions persists the pruned sessions.json after stopping."""
        old_time = datetime.now(timezone.utc) - timedelta(seconds=3600)
        s1 = _make_session(session_id="s1", pid=None, last_active_at=old_time, created_at=old_time)
        recent = _make_session(session_id="recent", pid=None)
        _seed_sessions_json(isolated_sessions_path, {"s1": s1, "recent": recent})
        manager = sm.SessionManager()

        await manager.gc_idle_sessions()

        data = json.loads(isolated_sessions_path.read_text(encoding="utf-8"))
        assert "s1" not in data
        assert "recent" in data


# ============================================================================
# O7-I35..I37: idle_gc_loop
# ============================================================================


class TestIdleGcLoop:
    """L3: idle_gc_loop handles CancelledError + Exception, sleeps before GC.

    B.1 O7 §7.7 invariants I35-I37: CancelledError breaks the loop;
    other Exceptions are logged + continue (loop doesn't die); first
    sleep(IDLE_GC_INTERVAL_S) THEN gc_idle_sessions (no immediate GC).
    """

    async def test_I35_cancelled_error_breaks_loop(self):
        """O7-I35: asyncio.CancelledError breaks the idle_gc_loop."""
        manager = sm.SessionManager()
        # Make sleep raise CancelledError on first call → loop breaks.
        with patch(
            "ppsspp_dfx_mcp.session.session_manager.asyncio.sleep",
            side_effect=asyncio.CancelledError,
        ):
            # Should exit cleanly without raising.
            await manager.idle_gc_loop()

    async def test_I36_other_exception_continues_loop(self):
        """O7-I36: non-CancelledError exceptions are logged + loop continues."""
        manager = sm.SessionManager()
        call_count = {"n": 0}

        async def fake_sleep(seconds: float) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First sleep succeeds → gc_idle_sessions called next.
                return
            # Second sleep raises CancelledError to exit loop.
            raise asyncio.CancelledError()

        async def fake_gc():
            raise RuntimeError("simulated GC failure")

        with patch(
            "ppsspp_dfx_mcp.session.session_manager.asyncio.sleep", fake_sleep
        ), patch.object(manager, "gc_idle_sessions", fake_gc):
            # Should NOT raise RuntimeError — loop continues.
            await manager.idle_gc_loop()

        assert call_count["n"] == 2  # one continue + one cancel

    async def test_I37_sleeps_before_first_gc(self):
        """O7-I37: idle_gc_loop awaits sleep(IDLE_GC_INTERVAL_S) BEFORE first gc.

        Verified by checking gc_idle_sessions is NOT called until after
        sleep resolves.
        """
        manager = sm.SessionManager()
        gc_call_order: list[str] = []
        sleep_call_order: list[str] = []

        async def fake_sleep(seconds: float) -> None:
            sleep_call_order.append("sleep")
            # Raise CancelledError to exit after first sleep.
            raise asyncio.CancelledError()

        async def fake_gc():
            gc_call_order.append("gc")

        with patch(
            "ppsspp_dfx_mcp.session.session_manager.asyncio.sleep", fake_sleep
        ), patch.object(manager, "gc_idle_sessions", fake_gc):
            await manager.idle_gc_loop()

        # sleep was called; gc was NOT called (sleep raised CancelledError
        # before gc_idle_sessions ran).
        assert sleep_call_order == ["sleep"]
        assert gc_call_order == []


# ============================================================================
# O7-I38: get_session_manager singleton
# ============================================================================


class TestGetSessionManager:
    """L3: get_session_manager is a lazy singleton factory.

    B.1 O7 §7.7 invariant I38: lazily constructs SessionManager on first
    call; subsequent calls return the same instance.
    """

    def test_I38a_singleton_returns_same_instance(self):
        """O7-I38: get_session_manager returns the same instance on repeated calls."""
        # Reset the module-level singleton.
        original = sm._default_manager
        try:
            sm._default_manager = None
            m1 = sm.get_session_manager()
            m2 = sm.get_session_manager()
            assert m1 is m2
        finally:
            sm._default_manager = original

    def test_I38b_lazy_construction(self):
        """O7-I38: get_session_manager constructs on first call (not at import)."""
        original = sm._default_manager
        try:
            sm._default_manager = None
            assert sm._default_manager is None
            m = sm.get_session_manager()
            assert sm._default_manager is m
            assert isinstance(m, sm.SessionManager)
        finally:
            sm._default_manager = original
