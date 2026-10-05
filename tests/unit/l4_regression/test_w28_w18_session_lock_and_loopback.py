"""W28/W18 regression guards.

**W28 (audit)** — ``SessionManager._start_once`` used to call
``await asyncio.to_thread(launcher.stop)`` while still holding the global
``self._lock`` (the shared load-modify-save lock). ``launcher.stop`` can take
~5s (terminate 3s + kill 2s; ``taskkill /F /T`` up to 10s on Windows), so a
port conflict on one start blocked every other session's
``list_sessions`` / ``get_session_state`` / saves for the whole duration. The
decision now happens inside the lock (pop the in-memory owner), the lock is
released, and only then does the slow stop run — mirroring ``stop_session`` /
``gc_idle_sessions``.

**W18 (audit)** — ``sessions.json`` supplies ``ws_url``, which drives OUTBOUND
connections; anyone able to write that file (path is env-overridable) could
point memory/input/state traffic at an arbitrary host. Both the loader and the
per-call fallback connect path now refuse non-loopback URLs via the single
shared predicate ``client_helper.is_loopback_ws_url``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ppsspp_dfx_mcp.core import proc as proc_mod
from ppsspp_dfx_mcp.errors import PortConflict, SessionNotFound, WsConnectFailed
from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session import client_helper
from ppsspp_dfx_mcp.session import session_manager as sm


@pytest.fixture(autouse=True)
def _fresh_singleton() -> Any:
    """The fallback tests use the module-level singleton for the session lock."""
    sm._default_manager = None
    yield
    sm._default_manager = None


def _make_iso(tmp_path: Path) -> Path:
    iso = tmp_path / "game.iso"
    iso.write_bytes(b"\x00" * 16)
    return iso


def _session(session_id: str, ws_url: str, *, pid: int | None = None) -> Session:
    return Session(session_id=session_id, iso_path="/test.iso", pid=pid, ws_url=ws_url)


def _write_sessions(path: Path, sessions: dict[str, Session]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({sid: sm._session_to_dict(s) for sid, s in sessions.items()}),
        encoding="utf-8",
    )


# ============================================================================
# W28 — slow launcher.stop must run OUTSIDE the global session lock
# ============================================================================


class _LockProbingLauncher:
    """Launcher double that records whether the global lock is held on stop.

    ``asyncio.to_thread`` runs ``stop`` in a worker thread, so reading
    ``manager._lock.locked()`` from there is a plain bool read of the lock's
    internal state — deterministic, no await race.
    """

    def __init__(self, ws_port_value: int = 12345) -> None:
        self.ws_port = ws_port_value
        self.start_calls = 0
        self.stop_calls = 0
        self.lock: Any = None
        self.lock_locked_during_stop: list[bool] = []

    async def start(self, iso_path: Path, extra_args: list[str] | None = None) -> Any:
        self.start_calls += 1
        return type("_StubProc", (), {"pid": 99999})()

    def stop(self) -> None:
        self.stop_calls += 1
        if self.lock is not None:
            self.lock_locked_during_stop.append(self.lock.locked())


async def test_port_conflict_stop_runs_outside_the_global_lock(
    isolated_sessions_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W28: the conflict cleanup must not hold ``self._lock`` during stop.

    Pre-fix this fails: ``await asyncio.to_thread(launcher.stop)`` sat inside
    ``async with self._lock``, so the lock was still owned when the worker
    thread ran ``stop`` and ``lock_locked_during_stop`` was ``[True]``. Post-fix
    the ``async with`` releases the lock before the stop, giving ``[False]``.
    """
    iso = _make_iso(tmp_path)
    stub = _LockProbingLauncher(ws_port_value=12345)
    # A LIVE session already owns port 12345 → the new start must conflict.
    _write_sessions(
        isolated_sessions_path,
        {"existing": _session("existing", "ws://127.0.0.1:12345/debugger", pid=4242)},
    )
    monkeypatch.setattr(proc_mod, "is_pid_alive", lambda _pid: True)

    with (
        patch("ppsspp_dfx_mcp.session.session_manager.PpssppLauncher", lambda: stub),
        pytest.raises(PortConflict) as exc_info,
    ):
        manager = sm.SessionManager()
        stub.lock = manager._lock
        await manager.start_session(str(iso))

    # Exactly one stop, with the lock NOT held, and no in-memory owner leaked.
    assert stub.stop_calls == 1
    assert stub.lock_locked_during_stop == [False], (
        "launcher.stop ran while the global session lock was held — a slow "
        "stop would block every other session's list/get/save (W28)."
    )
    assert manager._launchers == {}
    # The ORIGINAL conflict propagates (not a wrapped/replaced error).
    assert isinstance(exc_info.value, PortConflict)
    assert str(exc_info.value) == str(
        PortConflict(
            "port 12345 is already in use by active session existing (pid=4242). "
            "Use a different port or stop the existing session first.",
        )
    )


# ============================================================================
# W18 (1) — loader drops non-loopback / non-ws entries, keeps loopback
# ============================================================================

_NON_LOOPBACK_URLS = [
    "ws://10.0.0.5:12345/debugger",
    "ws://192.168.1.10:12345/debugger",
    "ws://evil.example.com:12345/debugger",
    "http://127.0.0.1:12345/debugger",  # scheme must be ws
]


@pytest.mark.parametrize("bad_url", _NON_LOOPBACK_URLS)
def test_loader_drops_non_loopback_entry_with_warning(
    isolated_sessions_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    bad_url: str,
) -> None:
    """W18: a non-loopback ws_url is DROPPED (loudly), never kept."""
    monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "")
    _write_sessions(
        isolated_sessions_path,
        {
            "evil": _session("evil", bad_url),
            "good": _session("good", "ws://127.0.0.1:12345/debugger"),
        },
    )

    with caplog.at_level(logging.WARNING):
        loaded = sm._load_sessions()

    assert set(loaded) == {"good"}, "only the loopback entry may survive"
    assert "evil" in caplog.text, "the dropped entry must be named in the log"
    assert bad_url in caplog.text, "the offending ws_url must be named"
    assert "not a loopback" in caplog.text


@pytest.mark.parametrize(
    "good_url",
    [
        "ws://127.0.0.1:12345/debugger",
        "ws://localhost:12345/debugger",
        "ws://LocalHost:12345/debugger",
        "ws://[::1]:12345/debugger",
    ],
)
def test_loader_keeps_loopback_entries(
    isolated_sessions_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    good_url: str,
) -> None:
    """Guard against over-blocking: every loopback spelling still loads."""
    monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "")
    _write_sessions(isolated_sessions_path, {"good": _session("good", good_url)})

    loaded = sm._load_sessions()

    assert set(loaded) == {"good"}


def test_loader_fake_sentinel_exempt_only_in_fake_mode(
    isolated_sessions_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fake-mode sentinel loads in fake mode and is dropped in real mode."""
    _write_sessions(isolated_sessions_path, {"f": _session("f", "fake://test")})

    monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "fake")
    assert set(sm._load_sessions()) == {"f"}

    monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "")
    assert sm._load_sessions() == {}


def test_loader_keeps_configured_non_loopback_host(
    isolated_sessions_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W18 refinement: the CONFIGURED non-loopback host is legitimate.

    ``PPSSPP_DFX_WS_HOST`` is documented, so an operator may deliberately run
    PPSSPP on a LAN machine. Their own persisted session must survive a restart;
    dropping it was a functional regression. The allowlist is therefore
    "loopback ∪ the configured ws_host()" — any OTHER non-loopback host is
    still dropped.
    """
    monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "")
    monkeypatch.setenv("PPSSPP_DFX_WS_HOST", "10.0.0.5")
    _write_sessions(
        isolated_sessions_path,
        {
            "match": _session("match", "ws://10.0.0.5:12345/debugger"),
            "other": _session("other", "ws://10.0.0.9:12345/debugger"),
        },
    )

    with caplog.at_level(logging.INFO):
        loaded = sm._load_sessions()

    assert set(loaded) == {"match"}, (
        "only the entry matching the configured non-loopback ws_host() may load"
    )
    # The acceptance is logged so an operator can see WHY it was allowed.
    assert "PPSSPP_DFX_WS_HOST" in caplog.text
    assert "10.0.0.5" in caplog.text
    # A different non-loopback host is still dropped (loudly).
    assert "other" in caplog.text
    assert "not a loopback" in caplog.text


def test_is_loopback_ws_url_matches_loader_contract() -> None:
    """The shared predicate itself: loopback spellings true, others false."""
    assert client_helper.is_loopback_ws_url("ws://127.0.0.1:1/debugger")
    assert client_helper.is_loopback_ws_url("ws://[::1]:1/debugger")
    assert client_helper.is_loopback_ws_url("ws://localhost:1/debugger")
    assert not client_helper.is_loopback_ws_url("ws://10.0.0.5:1/debugger")
    assert not client_helper.is_loopback_ws_url("http://127.0.0.1:1/debugger")
    assert not client_helper.is_loopback_ws_url("fake://test")
    assert not client_helper.is_loopback_ws_url("")


def test_is_allowed_ws_url_allows_configured_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extended predicate: loopback ∪ configured host; scheme still ws."""
    monkeypatch.setenv("PPSSPP_DFX_WS_HOST", "10.0.0.5")
    assert client_helper.is_allowed_ws_url("ws://10.0.0.5:1/debugger")
    assert client_helper.is_allowed_ws_url("ws://127.0.0.1:1/debugger")
    # A different non-loopback host stays refused.
    assert not client_helper.is_allowed_ws_url("ws://10.0.0.9:1/debugger")
    # Scheme/host constraints are not relaxed.
    assert not client_helper.is_allowed_ws_url("http://10.0.0.5:1/debugger")
    assert not client_helper.is_allowed_ws_url("")
    # With the default (loopback) ws_host, a LAN host is refused again.
    monkeypatch.setenv("PPSSPP_DFX_WS_HOST", "127.0.0.1")
    assert not client_helper.is_allowed_ws_url("ws://10.0.0.5:1/debugger")


# ============================================================================
# W18 (2) — fallback connect path refuses a non-loopback host
# ============================================================================


def _force_fallback(monkeypatch: pytest.MonkeyPatch, sess: Session) -> list[tuple[str, int]]:
    """Make ``session_client_with_transport`` take the per-call fallback.

    The loader normally drops non-loopback entries, so the fallback guard is
    exercised by injecting the session state directly and forcing
    ``get_transport`` / ``get_observer`` to report "no session-level handles".
    Returns the list the WsTransport factory records into (so a test can assert
    the transport was NOT constructed).
    """
    monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "")

    async def _state(_sid: str) -> Session:
        return sess

    async def _touch(_sid: str) -> Session:
        return sess

    async def _no_transport(_sid: str) -> Any:
        raise SessionNotFound("no session transport")

    async def _no_observer(_sid: str) -> Any:
        raise SessionNotFound("no observer")

    monkeypatch.setattr(sm, "get_session_state", _state)
    monkeypatch.setattr(sm, "touch_session", _touch)
    monkeypatch.setattr(sm, "get_transport", _no_transport)
    monkeypatch.setattr(sm, "get_observer", _no_observer)
    return []


async def test_fallback_refuses_non_loopback_ws_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W18: the fallback must not dial a non-loopback host.

    Pre-fix this fails: without the guard, ``WsTransport(host, port)`` is
    constructed against ``10.0.0.5`` and the call proceeds.
    """
    bad = _session("s-bad", "ws://10.0.0.5:12345/debugger")
    built = _force_fallback(monkeypatch, bad)

    def _factory(host: str, port: int) -> Any:
        built.append((host, port))
        raise AssertionError("WsTransport must not be constructed for a bad host")

    monkeypatch.setattr(client_helper, "WsTransport", _factory)

    with pytest.raises(WsConnectFailed) as exc_info:
        async with client_helper.session_client_with_transport("s-bad"):
            pass  # pragma: no cover — the fallback refuses before yielding

    assert built == [], "no transport may be built for a non-loopback host"
    msg = str(exc_info.value)
    assert "not loopback" in msg
    assert "10.0.0.5" in msg
    assert "recreate the session" in msg


async def test_fallback_loopback_still_connects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guard against over-blocking: a loopback fallback still connects."""
    good = _session("s-good", "ws://127.0.0.1:23456/debugger")
    _force_fallback(monkeypatch, good)

    calls: dict[str, Any] = {}

    class _StubWs:
        def __init__(self, host: str, port: int) -> None:
            calls["host"] = host
            calls["port"] = port

        async def connect(self) -> None:
            calls["connected"] = True

        async def send_version(self) -> None:
            calls["version"] = True

        async def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(client_helper, "WsTransport", _StubWs)

    async with client_helper.session_client_with_transport("s-good") as (
        _client,
        transport,
    ):
        assert isinstance(transport, _StubWs)
        assert calls["connected"] is True

    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 23456
    assert calls["version"] is True
    assert calls["closed"] is True


async def test_fallback_accepts_configured_non_loopback_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W18 refinement: the fallback dials the CONFIGURED non-loopback host.

    An operator who sets ``PPSSPP_DFX_WS_HOST=10.0.0.5`` must be able to use
    their own persisted session after a restart, so the fallback allowlist is
    "loopback ∪ ws_host()".
    """
    monkeypatch.setenv("PPSSPP_DFX_WS_HOST", "10.0.0.5")
    good = _session("s-cfg", "ws://10.0.0.5:23456/debugger")
    _force_fallback(monkeypatch, good)

    calls: dict[str, Any] = {}

    class _StubWs:
        def __init__(self, host: str, port: int) -> None:
            calls["host"] = host
            calls["port"] = port

        async def connect(self) -> None:
            calls["connected"] = True

        async def send_version(self) -> None:
            calls["version"] = True

        async def close(self) -> None:
            calls["closed"] = True

    monkeypatch.setattr(client_helper, "WsTransport", _StubWs)

    async with client_helper.session_client_with_transport("s-cfg") as (
        _client,
        transport,
    ):
        assert isinstance(transport, _StubWs)
        assert calls["connected"] is True

    assert calls["host"] == "10.0.0.5"
    assert calls["port"] == 23456
    assert calls["version"] is True
    assert calls["closed"] is True


async def test_fallback_refuses_other_host_when_configured_host_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DIFFERENT non-loopback host is still refused while one is configured."""
    monkeypatch.setenv("PPSSPP_DFX_WS_HOST", "10.0.0.5")
    bad = _session("s-other", "ws://10.0.0.9:12345/debugger")
    built = _force_fallback(monkeypatch, bad)

    def _factory(host: str, port: int) -> Any:
        built.append((host, port))
        raise AssertionError("WsTransport must not be constructed for a bad host")

    monkeypatch.setattr(client_helper, "WsTransport", _factory)

    with pytest.raises(WsConnectFailed) as exc_info:
        async with client_helper.session_client_with_transport("s-other"):
            pass  # pragma: no cover — the fallback refuses before yielding

    assert built == [], "no transport may be built for a non-configured host"
    assert "10.0.0.9" in str(exc_info.value)
