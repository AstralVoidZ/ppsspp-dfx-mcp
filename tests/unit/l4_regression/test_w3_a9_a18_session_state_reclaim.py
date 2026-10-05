"""L4 regression: per-session MCP state is reclaimed (W3/A9/A18, review v4).

W3: ``tools.state_observer``'s probe registry + seed latch and
``tools._common``'s zero-streak history are keyed by session id and had no
deletion path, so a long-lived server kept one entry per session it had ever
seen (all three containers only ever grew). A9: the fake-mode transport cache
in ``session.client_helper`` had the same shape.

A18: ``gc_idle_sessions`` popped launchers/transports/observers in its whole
phase-1 sweep into local dicts before the (slow) phase-2 kills. The lifespan
cancels that task on shutdown, and a cancellation mid-phase-2 destroyed the
local dicts with the unprocessed entries still inside them — the next scan
found only ``None`` in the tables and could never retry them.

These guards pin the reclamation contract: one shared helper clears every
module-level per-session table, it is wired at *both* reclamation points, and
GC drops a session only after that session's own kill succeeded.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from datetime import UTC, datetime, timedelta

import pytest
from _support import state as state_seam  # T053 S-4：集中式测试支撑缝

from ppsspp_dfx_mcp.core import cond_filter
from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session import client_helper
from ppsspp_dfx_mcp.session import session_manager as sm
from ppsspp_dfx_mcp.tools import _common, state_observer


def _session(session_id: str, *, pid: int | None = None) -> Session:
    """Build an expired-by-idle-time Session for the GC scan."""
    old = datetime.now(UTC) - timedelta(seconds=3600)
    return Session(
        session_id=session_id,
        iso_path="/tmp/fake.iso",
        pid=pid,
        ws_url="ws://127.0.0.1:12345/debugger",
        created_at=old,
        last_active_at=old,
        exec_count=0,
    )


def _seed_side_tables(session_id: str) -> None:
    """Put one entry for session_id into every module-level session table."""
    # T053 S-4：种子写入收敛到支撑缝（tests/_support/state.py）。
    state_seam.seed_all_side_tables(session_id)


def _side_table_has(session_id: str) -> bool:
    """True when session_id is still present in any module-level table."""
    return (
        session_id in state_observer._REGISTRY_BY_SESSION
        or session_id in state_observer._SEEDED_BY_SESSION
        or session_id in _common._ZERO_STREAKS
        or session_id in client_helper._FAKE_TRANSPORTS
        or any(sid == session_id for sid, _addr in cond_filter._filters)
    )


@pytest.fixture(autouse=True)
def _restore_side_tables():
    """Snapshot/restore the process-global tables so tests cannot leak."""
    # T053 S-4：快照/恢复的容器写入收敛到支撑缝，不再经局部别名直写。
    snap = state_seam.snapshot_side_tables()
    yield
    state_seam.restore_side_tables(snap)


class _AsyncCloseable:
    """Stand-in for a session-level transport (close) or observer (stop)."""

    def __init__(self) -> None:
        self.stop_calls = 0
        self.close_calls = 0

    async def stop(self) -> None:
        self.stop_calls += 1

    async def close(self) -> None:
        self.close_calls += 1


class _RecordingLauncher:
    """Launcher stand-in; ``stop`` is sync (session_manager offloads it)."""

    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


class _BlockingLauncher:
    """Launcher whose ``stop`` blocks until the test releases the gate."""

    def __init__(self, gate: threading.Event) -> None:
        self.gate = gate
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1
        self.gate.wait(10.0)


# ============================================================================
# W3/A9: the per-session tables have a deletion path
# ============================================================================


class TestDropSessionSideTables:
    """L4: one helper clears every module-level per-session table."""

    def test_state_observer_drop_session_clears_only_that_session(self):
        """W3: state_observer.drop_session removes exactly one session."""
        _seed_side_tables("w3-drop")
        _seed_side_tables("w3-keep")

        state_observer.drop_session("w3-drop")

        assert "w3-drop" not in state_observer._REGISTRY_BY_SESSION
        assert "w3-drop" not in state_observer._SEEDED_BY_SESSION
        assert state_observer._REGISTRY_BY_SESSION["w3-keep"] is not None
        assert "w3-keep" in state_observer._SEEDED_BY_SESSION

    def test_shared_helper_clears_every_table(self):
        """W3/A9: _drop_session_side_tables clears all four containers."""
        _seed_side_tables("w3-drop")
        _seed_side_tables("w3-keep")

        sm._drop_session_side_tables("w3-drop")

        assert not _side_table_has("w3-drop")
        assert _side_table_has("w3-keep")

    def test_helper_is_idempotent_for_unknown_session(self):
        """W3: reclaiming an unknown id is a no-op, not an error.

        Falsifiable: the unknown id must stay absent, and a *seeded* unrelated
        session must survive both calls -- so a helper that over-reaches
        (e.g. clears on a wildcard) fails here. The previous version called
        twice with no state to observe and asserted nothing.
        """
        _seed_side_tables("w3-unrelated")

        sm._drop_session_side_tables("w3-never-seen")
        sm._drop_session_side_tables("w3-never-seen")

        assert not _side_table_has("w3-never-seen")
        assert _side_table_has("w3-unrelated")


class TestReclaimWiring:
    """L4: both reclamation points call the shared helper (anti-drift)."""

    def test_stop_session_calls_the_shared_helper(self):
        """W3/A9: stop_session reclaims the module-level tables too."""
        src = inspect.getsource(sm.SessionManager.stop_session)
        assert "_drop_session_side_tables(session_id)" in src

    def test_gc_snapshots_and_drops_per_session(self):
        """A18: GC snapshots in phase 1 and drops per session in phase 2."""
        src = inspect.getsource(sm.SessionManager.gc_idle_sessions)
        # Phase 1 only snapshots — no wholesale pop into local dicts.
        assert "expired_launchers[sid] = self._launchers.get(sid)" in src
        assert "expired_transports[sid] = self._transports.get(sid)" in src
        # Phase 2 drops the reclaimed session's entries + side tables.
        assert "_drop_session_side_tables(sid)" in src


class TestStopSessionReclaimsSideTables:
    """W3/A9: end-to-end through the real stop_session path."""

    async def test_stop_session_leaves_no_trace_in_side_tables(self, monkeypatch):
        """W3/A9: after stop_session no container holds the session id."""
        _seed_side_tables("w3-stop")
        _seed_side_tables("w3-untouched")

        manager = sm.SessionManager()
        launcher = _RecordingLauncher()
        transport = _AsyncCloseable()
        observer = _AsyncCloseable()
        manager._launchers["w3-stop"] = launcher
        manager._transports["w3-stop"] = transport
        manager._observers["w3-stop"] = observer

        async def fake_load() -> dict[str, Session]:
            return {"w3-stop": _session("w3-stop")}

        async def fake_save(_sessions: dict[str, Session]) -> None:
            return None

        monkeypatch.setattr(sm, "_load_sessions_async", fake_load)
        monkeypatch.setattr(sm, "_save_sessions_async", fake_save)

        await manager.stop_session("w3-stop")

        assert launcher.stop_calls == 1
        assert observer.stop_calls == 1
        assert transport.close_calls == 1
        assert not _side_table_has("w3-stop")
        assert _side_table_has("w3-untouched")


# ============================================================================
# A18: a cancelled GC keeps unprocessed sessions retryable
# ============================================================================


class TestCancelledGcKeepsUnprocessedSessions:
    """L4: cancelling GC mid-phase-2 must not strand entries invisibly."""

    async def test_cancelled_gc_keeps_unprocessed_sessions_retryable(self, monkeypatch):
        """A18: s2 is still in the tables after s1's stop is cancelled."""
        gate = threading.Event()
        launcher = _BlockingLauncher(gate)
        s1_transport, s1_observer = _AsyncCloseable(), _AsyncCloseable()
        s2_transport, s2_observer = _AsyncCloseable(), _AsyncCloseable()

        manager = sm.SessionManager()
        manager._launchers = {"a18-s1": launcher, "a18-s2": _RecordingLauncher()}
        manager._transports = {"a18-s1": s1_transport, "a18-s2": s2_transport}
        manager._observers = {"a18-s1": s1_observer, "a18-s2": s2_observer}

        async def fake_load() -> dict[str, Session]:
            return {"a18-s1": _session("a18-s1"), "a18-s2": _session("a18-s2")}

        monkeypatch.setattr(sm, "_load_sessions_async", fake_load)

        task = asyncio.create_task(manager.gc_idle_sessions())
        try:
            for _ in range(500):
                if launcher.stop_calls:
                    break
                await asyncio.sleep(0.01)
            assert launcher.stop_calls == 1, "phase 2 never reached launcher.stop"
            # The lifespan cancels the GC task on shutdown.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            gate.set()

        # The in-flight session was not reclaimed: keep it for the next cycle.
        assert manager._launchers["a18-s1"] is launcher
        assert manager._transports["a18-s1"] is s1_transport
        assert manager._observers["a18-s1"] is s1_observer
        # And the never-reached session must still be visible to the next scan.
        # Before A18 the wholesale phase-1 pop had already removed every entry.
        assert manager._launchers["a18-s2"] is not None
        assert manager._transports["a18-s2"] is s2_transport
        assert manager._observers["a18-s2"] is s2_observer
