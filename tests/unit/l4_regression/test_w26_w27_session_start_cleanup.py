"""W26/W27 regression guards: a start attempt must never orphan a process and
must never leave a session-level transport/observer without an owner.

**W26 (review v4)** — ``_start_once`` used to register
``_launchers[session_id]`` only AFTER persisting sessions.json. Every await in
between (session-file load/save, WS probe, transport establishment) is a
cancellation point, and the client cancels a start after ~30s while an unpinned
boot was measured at 31.9s, so the window is reachable. A cancel landing there
left the PPSSPP process with no in-memory owner: ``_teardown_wedged_attempt``
and ``stop_session`` both popped ``None``, and ``gc_idle_sessions`` only scans
sessions.json, which that attempt had not reached. The launcher is now
registered immediately after the spawn, and every abort path stops it and drops
the record.

**W27 (review v4)** — a concurrent ``stop_session`` pops ``_transports`` /
``_observers`` under ``self._lock`` but does not hold the session lock, so the
window between the WS probe and the two registration writes was real. Writing
unconditionally resurrected the entries of a session that had just been removed,
leaving a live WS (with its ``_recv_loop``) and the observer's background tasks
with no owner for the lifetime of the process. The registration now re-checks
liveness inside the same lock and aborts the establishment when the session is
gone.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ppsspp_dfx_mcp.session import session_manager as sm


class _StubLauncher:
    """Minimal PpssppLauncher double: spawn/stop bookkeeping only."""

    def __init__(self, ws_port_value: int | None = 12345) -> None:
        self.ws_port = ws_port_value
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self, iso_path: Path, extra_args: list[str] | None = None) -> Any:
        self.start_calls += 1
        return type("_StubProc", (), {"pid": 99999})()

    def stop(self) -> None:
        self.stop_calls += 1


def _make_iso(tmp_path: Path) -> Path:
    iso = tmp_path / "game.iso"
    iso.write_bytes(b"\x00" * 16)
    return iso


async def _raise_cancelled(*args: Any, **kwargs: Any) -> None:
    """Stand-in for an await that observes task cancellation."""
    raise asyncio.CancelledError


class TestW26AbortedStartNeverOrphansTheProcess:
    """A cancel/failure after the spawn must still stop the process."""

    async def test_cancellation_at_persist_stops_the_process(
        self, isolated_sessions_path: Path, tmp_path: Path
    ) -> None:
        """The exact W26 window: cancelled while sessions.json is written.

        Before the fix the launcher was not registered yet, so nothing stopped
        the freshly spawned process and nothing could ever find it again.
        """
        iso = _make_iso(tmp_path)
        stub = _StubLauncher()

        with (
            patch("ppsspp_dfx_mcp.session.session_manager.PpssppLauncher", lambda: stub),
            patch(
                "ppsspp_dfx_mcp.session.session_manager._save_sessions_async",
                _raise_cancelled,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            manager = sm.SessionManager()
            await manager.start_session(str(iso))

        assert stub.start_calls == 1
        # The process was stopped and no stale owner survived the abort.
        assert stub.stop_calls == 1
        assert manager._launchers == {}
        assert not isolated_sessions_path.exists()

    async def test_resilient_cancellation_at_persist_stops_the_process(
        self, isolated_sessions_path: Path, tmp_path: Path
    ) -> None:
        """Same window on the resilient path.

        The ``except BaseException`` guard existed before the fix but could not
        help: it called ``_teardown_wedged_attempt``, which popped ``None``
        because the launcher had never been registered.
        """
        iso = _make_iso(tmp_path)
        stub = _StubLauncher()

        with (
            patch("ppsspp_dfx_mcp.session.session_manager.PpssppLauncher", lambda: stub),
            patch(
                "ppsspp_dfx_mcp.session.session_manager._save_sessions_async",
                _raise_cancelled,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            manager = sm.SessionManager()
            await manager.start_session(str(iso), resilient=True)

        assert stub.stop_calls == 1
        assert manager._launchers == {}

    async def test_aborted_start_leaves_no_session_record(
        self, isolated_sessions_path: Path, tmp_path: Path
    ) -> None:
        """A cancelled start must not leave a phantom session behind either."""
        iso = _make_iso(tmp_path)
        stub = _StubLauncher()

        with (
            patch("ppsspp_dfx_mcp.session.session_manager.PpssppLauncher", lambda: stub),
            patch(
                "ppsspp_dfx_mcp.session.session_manager._save_sessions_async",
                _raise_cancelled,
            ),
            pytest.raises(asyncio.CancelledError),
        ):
            manager = sm.SessionManager()
            await manager.start_session(str(iso), resilient=True)

        # No process, no launcher, no record: nothing left to leak or to be
        # mistaken for a running session.
        assert stub.stop_calls == 1
        assert manager._launchers == {}
        assert not isolated_sessions_path.exists()


class _FakeSessionTransport:
    """Transport double that records close() and can fire a hook on send."""

    def __init__(self, on_send: Callable[[], None] | None = None) -> None:
        self.version_info: dict[str, Any] = {"name": "PPSSPP", "version": "v1.20"}
        self.closed = False
        self._on_send = on_send

    async def connect(self) -> None:
        return None

    async def send_version(self) -> None:
        if self._on_send is not None:
            self._on_send()

    async def close(self) -> None:
        self.closed = True


class _FakeObserver:
    """GameStateObserver double: records start/stop only."""

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class TestW27EstablishmentRechecksLiveness:
    """The transport/observer registration must not resurrect a stopped session."""

    @staticmethod
    def _patch_establishment(
        monkeypatch: pytest.MonkeyPatch,
        transports: list[_FakeSessionTransport],
        observers: list[_FakeObserver],
        *,
        on_send: Callable[[], None] | None = None,
    ) -> None:
        async def _probe_true(ws_url: str) -> bool:
            return True

        def _make_transport(*args: Any, **kwargs: Any) -> _FakeSessionTransport:
            transport = _FakeSessionTransport(on_send=on_send)
            transports.append(transport)
            return transport

        def _make_observer(transport: Any) -> _FakeObserver:
            observer = _FakeObserver(transport)
            observers.append(observer)
            return observer

        monkeypatch.setattr(sm, "_probe_ws_connection", _probe_true)
        # The establishment imports both symbols lazily at call time, so
        # patching the defining modules is what the production import sees.
        monkeypatch.setattr("ppsspp_dfx_mcp.core.transport.WsTransport", _make_transport)
        monkeypatch.setattr(
            "ppsspp_dfx_mcp.core.game_state_observer.GameStateObserver",
            _make_observer,
        )

    async def test_stop_during_establishment_does_not_resurrect_entries(
        self, isolated_sessions_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """W27: a stop landing between the probe and the writes must win."""
        iso = _make_iso(tmp_path)
        stub = _StubLauncher()
        transports: list[_FakeSessionTransport] = []
        observers: list[_FakeObserver] = []

        def _concurrent_stop() -> None:
            # stop_session phase 1+3: the record is gone by the time the
            # establishment re-checks liveness.
            sm._save_sessions({})

        self._patch_establishment(monkeypatch, transports, observers, on_send=_concurrent_stop)

        with (
            patch("ppsspp_dfx_mcp.session.session_manager.PpssppLauncher", lambda: stub),
            pytest.raises(sm.SessionNotFound),
        ):
            manager = sm.SessionManager()
            await manager.start_session(str(iso))

        # The local handles were closed instead of being registered.
        assert len(transports) == 1
        assert transports[0].closed is True
        assert len(observers) == 1
        assert observers[0].stopped is True
        assert manager._transports == {}
        assert manager._observers == {}
        # And the process the aborted start had spawned was stopped.
        assert stub.stop_calls == 1
        assert manager._launchers == {}

    async def test_live_session_still_registers_both_handles(
        self, isolated_sessions_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: the liveness re-check must not disable the registration."""
        iso = _make_iso(tmp_path)
        stub = _StubLauncher()
        transports: list[_FakeSessionTransport] = []
        observers: list[_FakeObserver] = []

        self._patch_establishment(monkeypatch, transports, observers)

        with patch("ppsspp_dfx_mcp.session.session_manager.PpssppLauncher", lambda: stub):
            manager = sm.SessionManager()
            sess = await manager.start_session(str(iso))

        assert len(manager._transports) == 1
        assert len(manager._observers) == 1
        assert transports[0].closed is False
        assert observers[0].stopped is False
        # The version fingerprint fold still happens (same critical section).
        stored = sm._load_sessions()[sess.session_id]
        assert stored.extra["ppsspp_version"] == {"name": "PPSSPP", "version": "v1.20"}
        # `restored` is in-memory provenance only (W4, review v2): it is stamped
        # by _load_sessions and must never reach the file, or a load-modify-save
        # would stamp it back onto a live in-process session.
        persisted = json.loads(isolated_sessions_path.read_text(encoding="utf-8"))
        assert "restored" not in persisted[sess.session_id]["extra"]
        assert persisted[sess.session_id]["extra"]["ppsspp_version"] == {
            "name": "PPSSPP",
            "version": "v1.20",
        }
