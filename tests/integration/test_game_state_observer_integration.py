"""Integration tests for fix-ppsspp-dfx-freeze-misjudgment (tasks 13.1-13.8).

Exercises the GameStateObserver + session-level transport lifecycle end-to-end
(without a real PPSSPP — uses FakeTransport + mock WsTransport as appropriate).

Test groups:
- 13.1: GameStateObserver state machine full chain (loading→running→paused→
  running→quit).
- 13.2: GameStateObserver illegal transitions (logged as warning, no
  exception).
- 13.3: Single-consumer dispatcher (multiple subscribers, no drain-and-
  requeue conflict).
- 13.4: Log broadcast injection (level 2 ERROR → logging.ERROR).
- 13.5: gpu.stats.feed frame freeze detection (3s + game running →
  CpuFreezeSuspected; game paused → not triggered).
- 13.6: gpu.stats.get broadcast with unmatched ticket falls back to events
  queue (M5 regression test).
- 13.7: Session-level transport lifecycle (start establishes, stop closes,
  multiple calls reuse).
- 13.8: client_helper.session_client_with_transport reuses session-level
  transport (no per-call close).

"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fake_transport import FakeTransport
from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver
from ppsspp_dfx_mcp.errors import (
    CpuFreezeSuspected,
    SessionNotFound,
)
from ppsspp_dfx_mcp.session import session_manager as sm_mod
from ppsspp_dfx_mcp.session.client_helper import session_client_with_transport


# ============================================================================
# Shared fixtures
# ============================================================================


@pytest.fixture
async def observer_with_transport():
    """Yield a (observer, transport) pair, with observer started.

    The observer's dispatcher + log consumer are started. The transport
    is a FakeTransport — its `events` queue feeds the dispatcher.

    Teardown stops the observer (cancels all background coroutines).
    """
    transport = FakeTransport()
    transport.set_state({"stepping": False})
    # Configure broadcast.config.set response (called by observer.start()).
    transport.set_response("broadcast.config.set", {})

    observer = GameStateObserver(transport)
    await observer.start()
    try:
        yield observer, transport
    finally:
        await observer.stop()


# ============================================================================
# 13.1 — GameStateObserver state machine full chain
# ============================================================================


class TestGameStateMachineFullChain:
    """13.1: GameStateObserver state machine transitions
    loading→running→paused→running→quit (full chain).

    Each transition is driven by pushing the corresponding broadcast onto
    the transport's events queue. The dispatcher consumes the queue and
    applies the state machine transition.
    """

    @pytest.mark.asyncio
    async def test_full_lifecycle_chain(self, observer_with_transport):
        """13.1: full chain loading→running→paused→running→quit."""
        observer, transport = observer_with_transport

        # Initial state is "loading".
        assert observer.get_state() == "loading"

        # loading → running (game.start).
        transport.push_broadcast({"event": "game.start"})
        await asyncio.sleep(0.05)  # let dispatcher consume
        assert observer.get_state() == "running"

        # running → paused (game.pause).
        transport.push_broadcast({"event": "game.pause"})
        await asyncio.sleep(0.05)
        assert observer.get_state() == "paused"

        # paused → running (game.resume).
        transport.push_broadcast({"event": "game.resume"})
        await asyncio.sleep(0.05)
        assert observer.get_state() == "running"

        # running → quit (game.quit).
        transport.push_broadcast({"event": "game.quit"})
        await asyncio.sleep(0.05)
        assert observer.get_state() == "quit"


# ============================================================================
# 13.2 — GameStateObserver illegal transitions
# ============================================================================


class TestGameStateMachineIllegalTransitions:
    """13.2: GameStateObserver illegal transitions log warning, no exception.

    Invalid transitions (e.g. game.pause while in loading state) are
    logged as warnings but do not raise — tolerates missed broadcasts.
    """

    @pytest.mark.asyncio
    async def test_illegal_transition_logs_warning_no_exception(
        self, observer_with_transport, caplog
    ):
        """13.2: game.pause while in loading state → warning logged, no
        exception, state remains loading.
        """
        observer, transport = observer_with_transport

        # Initial state is "loading" — game.pause is invalid from loading.
        assert observer.get_state() == "loading"

        with caplog.at_level(logging.WARNING, logger="ppsspp_dfx_mcp.core.game_state_observer"):
            transport.push_broadcast({"event": "game.pause"})
            await asyncio.sleep(0.05)

        # State remains loading (illegal transition rejected).
        assert observer.get_state() == "loading"
        # Warning was logged.
        assert any(
            "invalid state transition" in rec.message
            and "game.pause" in rec.message
            for rec in caplog.records
        ), (
            "Illegal transition must log a warning with the event name "
            "and current state."
        )

    @pytest.mark.asyncio
    async def test_illegal_transition_after_quit(
        self, observer_with_transport, caplog
    ):
        """13.2 (extra): game.start while in quit state → warning logged,
        state remains quit (quit is terminal until session reset).
        """
        observer, transport = observer_with_transport

        # Drive to quit state first.
        transport.push_broadcast({"event": "game.start"})
        await asyncio.sleep(0.05)
        transport.push_broadcast({"event": "game.quit"})
        await asyncio.sleep(0.05)
        assert observer.get_state() == "quit"

        # game.start from quit is illegal (quit is terminal).
        with caplog.at_level(logging.WARNING, logger="ppsspp_dfx_mcp.core.game_state_observer"):
            transport.push_broadcast({"event": "game.start"})
            await asyncio.sleep(0.05)

        assert observer.get_state() == "quit"
        assert any(
            "invalid state transition" in rec.message
            for rec in caplog.records
        )


# ============================================================================
# 13.3 — Single-consumer dispatcher (no drain-and-requeue conflict)
# ============================================================================


class TestSingleConsumerDispatcher:
    """13.3: Single-consumer dispatcher avoids drain-and-requeue conflict.

    Multiple subscribers waiting for different event_names concurrently
    should each receive their own events without one subscriber draining
    the other's messages.

    The dispatcher consumes `transport.events` once and dispatches to
    per-event-name asyncio.Queue instances — subscribers consume from
    their dedicated queues.
    """

    @pytest.mark.asyncio
    async def test_concurrent_subscribers_no_drain_conflict(
        self, observer_with_transport
    ):
        """13.3: concurrent wait_for_resume + game.pause subscriber both
        receive their events.

        Without the single-consumer dispatcher, two concurrent
        wait_for_broadcast calls would drain-and-requeue each other's
        events. The per-event-name queue pattern eliminates this.
        """
        observer, transport = observer_with_transport

        # Drive to running state first (so game.pause is valid).
        transport.push_broadcast({"event": "game.start"})
        await asyncio.sleep(0.05)
        assert observer.get_state() == "running"

        # Start two concurrent waiters: one for cpu.resume, one for
        # game.pause. Both wait on their dedicated per-event-name queues
        # (populated by the single-consumer dispatcher).
        resume_task = asyncio.ensure_future(
            observer.wait_for_resume(timeout_ms=500)
        )
        # game.pause subscriber — read directly from the game.pause queue.
        pause_task = asyncio.ensure_future(
            observer._queues["game.pause"].get()
        )
        await asyncio.sleep(0.02)  # let tasks register

        # Push both broadcasts in quick succession.
        transport.push_broadcast({"event": "cpu.resume"})
        transport.push_broadcast({"event": "game.pause"})

        # Both tasks should complete successfully (each got its event).
        try:
            await asyncio.wait_for(
                asyncio.gather(resume_task, pause_task),
                timeout=1.0,
            )
        except asyncio.TimeoutError:
            pytest.fail(
                "Concurrent subscribers timed out — dispatcher failed to "
                "route events to per-event-name queues (drain-and-requeue "
                "conflict suspected)."
            )

        # resume_task returned True (got cpu.resume broadcast).
        assert resume_task.result() is True
        # pause_task returned the game.pause message.
        assert pause_task.result()["event"] == "game.pause"
        # State machine transitioned to paused.
        assert observer.get_state() == "paused"


# ============================================================================
# 13.4 — Log broadcast injection
# ============================================================================


class TestLogBroadcastInjection:
    """13.4: log broadcasts are injected into the
    ``ppsspp_dfx_mcp.ppsspp_log`` Python logger with mapped level.

    Level mapping (game-state-broadcast/spec.md):
    - 1 (NOTICE) → logging.INFO
    - 2 (ERROR) → logging.ERROR
    - 3 (WARN) → logging.WARNING
    - 4 (INFO) → logging.INFO
    - 5 (DEBUG) → logging.DEBUG
    - 6 (VERBOSE) → logging.DEBUG
    """

    @pytest.mark.asyncio
    async def test_log_level_2_error_injected(
        self, observer_with_transport, caplog
    ):
        """13.4: level=2 (ERROR) broadcast → logging.ERROR."""
        observer, transport = observer_with_transport

        with caplog.at_level(
            logging.DEBUG, logger="ppsspp_dfx_mcp.ppsspp_log"
        ):
            transport.push_broadcast({
                "event": "log",
                "level": 2,
                "message": "sceKernelWaitSema timeout",
                "header": "HLE",
                "channel": "Kernel",
            })
            await asyncio.sleep(0.05)

        # Find the injected log record.
        error_records = [
            rec for rec in caplog.records
            if rec.levelno == logging.ERROR
            and "sceKernelWaitSema timeout" in rec.message
        ]
        assert len(error_records) == 1, (
            "Level 2 (ERROR) log broadcast must be injected as "
            "logging.ERROR via ppsspp_dfx_mcp.ppsspp_log logger."
        )
        # Message format: "[<channel>] <header>: <message>".
        assert "[Kernel] HLE: sceKernelWaitSema timeout" in error_records[0].message

    @pytest.mark.asyncio
    async def test_log_level_4_info_injected(
        self, observer_with_transport, caplog
    ):
        """13.4 (extra): level=4 (INFO) broadcast → logging.INFO."""
        observer, transport = observer_with_transport

        with caplog.at_level(
            logging.DEBUG, logger="ppsspp_dfx_mcp.ppsspp_log"
        ):
            transport.push_broadcast({
                "event": "log",
                "level": 4,
                "message": "loading module",
                "header": "Loader",
                "channel": "Load",
            })
            await asyncio.sleep(0.05)

        info_records = [
            rec for rec in caplog.records
            if rec.levelno == logging.INFO
            and "loading module" in rec.message
        ]
        assert len(info_records) == 1, (
            "Level 4 (INFO) log broadcast must be injected as logging.INFO."
        )

    @pytest.mark.asyncio
    async def test_malformed_log_broadcast_dropped_silently(
        self, observer_with_transport, caplog
    ):
        """13.4 (extra): malformed log broadcast (missing level/message)
        is silently dropped — no exception, no log record.
        """
        observer, transport = observer_with_transport

        with caplog.at_level(
            logging.DEBUG, logger="ppsspp_dfx_mcp.ppsspp_log"
        ):
            # Missing level and message — should be dropped silently.
            transport.push_broadcast({"event": "log"})
            await asyncio.sleep(0.05)

        # No log records injected (malformed broadcast dropped).
        log_records = [
            rec for rec in caplog.records
            if rec.name == "ppsspp_dfx_mcp.ppsspp_log"
        ]
        assert len(log_records) == 0, (
            "Malformed log broadcast (missing level/message) must be "
            "silently dropped — no log record injected."
        )


# ============================================================================
# 13.5 — gpu.stats.feed frame freeze detection
# ============================================================================


class TestGpuStatsFeedFreezeDetection:
    """13.5: gpu.stats.feed frame freeze detection integration test.

    - 3s no broadcast + game running → CpuFreezeSuspected.
    - game paused → not triggered (player paused, no frames expected).
    """

    @pytest.mark.asyncio
    async def test_freeze_detected_when_running_and_no_frames(
        self, observer_with_transport
    ):
        """13.5a: 3s+ no gpu.stats.get broadcast + game running →
        CpuFreezeSuspected raised by the freeze detector.

        Uses time manipulation to avoid actually waiting 3s.
        """
        observer, transport = observer_with_transport

        # Drive to running state.
        transport.push_broadcast({"event": "game.start"})
        await asyncio.sleep(0.05)
        assert observer.get_state() == "running"

        # Start gpu.stats.feed — this sets last_frame_at = now() and
        # starts the freeze detector coroutine.
        transport.set_response("gpu.stats.feed", {})
        await observer.start_gpu_stats_feed()

        # S4 fix: backdate the monotonic frame timestamp by 4 seconds
        # (>3s threshold).
        observer._last_frame_mono = time.monotonic() - 4

        # The freeze detector loops every 1.0s. Wait for it to detect.
        # Capture the CpuFreezeSuspected from the task's exception.
        freeze_task = observer._gpu_freeze_task
        assert freeze_task is not None

        try:
            await asyncio.wait_for(freeze_task, timeout=2.0)
        except CpuFreezeSuspected as exc:
            # Expected — freeze detected.
            assert "GPU freeze" in str(exc)
            assert "last_frame_at" in str(exc)
        except asyncio.TimeoutError:
            pytest.fail(
                "Freeze detector did not raise CpuFreezeSuspected after "
                "4s of no frames while game running."
            )
        except Exception as e:
            pytest.fail(f"Unexpected exception from freeze detector: {e!r}")

    @pytest.mark.asyncio
    async def test_no_freeze_when_paused_and_no_frames(
        self, observer_with_transport
    ):
        """13.5b: 3s+ no broadcast + game paused → no CpuFreezeSuspected
        (player paused, no frames expected).
        """
        observer, transport = observer_with_transport

        # Drive to paused state.
        transport.push_broadcast({"event": "game.start"})
        await asyncio.sleep(0.05)
        transport.push_broadcast({"event": "game.pause"})
        await asyncio.sleep(0.05)
        assert observer.get_state() == "paused"

        # Start gpu.stats.feed.
        transport.set_response("gpu.stats.feed", {})
        await observer.start_gpu_stats_feed()

        # S4 fix: backdate the monotonic frame timestamp by 5 seconds.
        observer._last_frame_mono = time.monotonic() - 5

        # Wait for >1s (freeze detector loop interval) — should NOT raise.
        await asyncio.sleep(1.5)

        # The freeze detector task should still be running (not raised).
        freeze_task = observer._gpu_freeze_task
        assert freeze_task is not None
        assert not freeze_task.done(), (
            "Freeze detector must NOT raise CpuFreezeSuspected when game "
            "is paused — player pause is expected to stop frame delivery."
        )
        # No exception stored on the task.
        if freeze_task.done():
            exc = freeze_task.exception()
            assert exc is None, f"Unexpected exception: {exc!r}"


# ============================================================================
# 13.6 — gpu.stats.get broadcast with unmatched ticket (M5 regression)
# ============================================================================


class TestGpuStatsGetBroadcastTicketFallback:
    """13.6: ``gpu.stats.get`` broadcast with ticket but no matching
    ``_pending`` future falls back to the events queue.

    M5 (game-state-broadcast/spec.md): the current ``_recv_loop`` in
    transport.py (lines 178-180, else branch) already handles this
    correctly — messages with a ticket that has no matching future go
    to the events queue. This test is a regression guard.
    """

    @pytest.mark.asyncio
    async def test_unmatched_ticket_message_goes_to_events_queue(self):
        """13.6: a message with a ticket not in ``_pending`` ends up on
        the ``events`` queue (regression test for M5).

        Simulates the _recv_loop's else-branch behavior by directly
        invoking the routing logic on a real WsTransport instance with
        a mocked ws attribute.
        """
        from websockets.protocol import State as WsState

        from ppsspp_dfx_mcp.core.transport import WsTransport

        transport = WsTransport("127.0.0.1", 12345)
        # Don't call connect() — we mock the ws attribute directly.

        unmatched_ticket_msg = {
            "event": "gpu.stats.get",
            "ticket": "t-orphan-99999",  # not in _pending
            "fps": 60,
        }

        class _FakeWs:
            def __init__(self):
                self._messages = [json.dumps(unmatched_ticket_msg)]
                # Use the real websockets.State enum so _recv_loop's
                # condition `self.ws.state == State.OPEN` is True.
                self.state = WsState.OPEN

            async def recv(self):
                if not self._messages:
                    # Set state to CLOSED so the loop condition fails on
                    # the next iteration, then raise asyncio.TimeoutError
                    # (caught by the loop's except, which continues to
                    # the while check). This cleanly exits the loop.
                    self.state = WsState.CLOSED
                    raise asyncio.TimeoutError
                return self._messages.pop(0)

            async def close(self):
                pass

        transport.ws = _FakeWs()

        # Run _recv_loop — it should consume the message and route it to
        # the events queue (else branch), then exit cleanly.
        try:
            await asyncio.wait_for(transport._recv_loop(), timeout=2.0)
        except Exception:
            pass

        # The unmatched-ticket message should now be on the events queue.
        try:
            msg = await asyncio.wait_for(
                transport.events.get(), timeout=0.5
            )
        except asyncio.TimeoutError:
            pytest.fail(
                "Unmatched-ticket message was NOT routed to the events "
                "queue — _recv_loop's else branch (M5) is broken."
            )

        assert msg["event"] == "gpu.stats.get"
        assert msg["ticket"] == "t-orphan-99999"
        assert msg["fps"] == 60


# ============================================================================
# 13.7 — Session-level transport lifecycle
# ============================================================================


class TestSessionLevelTransportLifecycle:
    """13.7: session-level transport lifecycle integration test.

    - session start establishes transport + starts observer.
    - multiple tool calls reuse the same transport (no rebuild).
    - session stop closes transport + observer cancels all coroutines.

    Uses direct injection into SessionManager._transports / _observers
    to simulate the bind phase (real start_session requires a live
    PPSSPP process which is unavailable in tests).
    """

    @pytest.mark.asyncio
    async def test_get_transport_returns_session_level_transport(
        self, isolated_sessions_path: Path
    ):
        """13.7a: get_transport returns the session-level transport
        injected by start_session (bind phase).
        """
        manager = sm_mod.SessionManager()
        transport = FakeTransport()
        # Simulate the bind phase by directly injecting.
        manager._transports["test-sess-1"] = transport

        result = await manager.get_transport("test-sess-1")

        assert result is transport, (
            "get_transport must return the session-level transport "
            "injected by start_session (bind phase)."
        )

    @pytest.mark.asyncio
    async def test_get_transport_raises_for_unknown_session(
        self, isolated_sessions_path: Path
    ):
        """13.7b: get_transport raises SessionNotFound for unknown session."""
        manager = sm_mod.SessionManager()

        with pytest.raises(SessionNotFound):
            await manager.get_transport("unknown-sess")

    @pytest.mark.asyncio
    async def test_get_observer_returns_session_level_observer(
        self, isolated_sessions_path: Path
    ):
        """13.7c: get_observer returns the session-level observer
        injected by start_session (bind phase).
        """
        manager = sm_mod.SessionManager()
        transport = FakeTransport()
        observer = GameStateObserver(transport)
        manager._observers["test-sess-2"] = observer

        result = await manager.get_observer("test-sess-2")

        assert result is observer

    @pytest.mark.asyncio
    async def test_stop_session_closes_transport_and_observer(
        self, isolated_sessions_path: Path
    ):
        """13.7d: stop_session calls observer.stop() + transport.close()
        and removes both from the in-memory dicts (close phase).
        """
        manager = sm_mod.SessionManager()

        # Build a session and inject transport + observer.
        from ppsspp_dfx_mcp.models.session import Session
        from datetime import datetime, timezone

        sess = Session(
            session_id="test-sess-3",
            iso_path="/tmp/fake.iso",
            pid=None,  # no real process to kill
            ws_url="ws://127.0.0.1:12345/debugger",
            created_at=datetime.now(timezone.utc),
            last_active_at=datetime.now(timezone.utc),
            exec_count=0,
            ws_connected=False,
        )
        # Seed sessions.json.
        sm_mod._save_sessions({"test-sess-3": sess})

        # Inject mock transport + observer.
        transport = AsyncMock()
        observer = AsyncMock()
        manager._transports["test-sess-3"] = transport
        manager._observers["test-sess-3"] = observer

        # stop_session should call observer.stop() + transport.close().
        await manager.stop_session("test-sess-3")

        observer.stop.assert_awaited_once()
        transport.close.assert_awaited_once()
        # Both removed from in-memory dicts.
        assert "test-sess-3" not in manager._transports
        assert "test-sess-3" not in manager._observers

    @pytest.mark.asyncio
    async def test_multiple_calls_reuse_same_transport(
        self, isolated_sessions_path: Path
    ):
        """13.7e: multiple get_transport calls return the same instance
        (no rebuild between calls — accept phase reuses the bound socket).
        """
        manager = sm_mod.SessionManager()
        transport = FakeTransport()
        manager._transports["test-sess-4"] = transport

        t1 = await manager.get_transport("test-sess-4")
        t2 = await manager.get_transport("test-sess-4")
        t3 = await manager.get_transport("test-sess-4")

        assert t1 is transport
        assert t2 is transport
        assert t3 is transport
        # Same instance — not rebuilt.
        assert t1 is t2 is t3


# ============================================================================
# 13.8 — client_helper.session_client_with_transport reuse
# ============================================================================


class TestSessionClientWithTransportReuse:
    """13.8: ``client_helper.session_client_with_transport`` reuses the
    session-level transport (accept phase) and does NOT close it on exit
    (session stop owns the transport lifecycle).
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        """Reset the session_manager singleton before each test.

        ``session_client_with_transport`` calls module-level
        ``session_manager.get_transport`` / ``get_observer`` which
        delegate to ``get_session_manager()`` (the singleton). Tests
        must inject into the singleton's ``_transports`` / ``_observers``
        — not a fresh ``SessionManager()`` instance.
        """
        sm_mod._default_manager = None
        yield
        sm_mod._default_manager = None

    @pytest.mark.asyncio
    async def test_reuses_session_level_transport(
        self, isolated_sessions_path: Path, monkeypatch
    ):
        """13.8a: session_client_with_transport retrieves the session-level
        transport via get_transport + get_observer, constructs
        PpssppDebugClient with them, and does NOT close the transport on exit.
        """
        from ppsspp_dfx_mcp.models.session import Session
        from datetime import datetime, timezone

        # Ensure production path (not fake test mode).
        monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "")

        # Use the singleton — session_client_with_transport calls
        # module-level get_transport/get_observer which use
        # get_session_manager().
        manager = sm_mod.get_session_manager()

        # Seed a session (no real PID — no process to kill).
        sess = Session(
            session_id="test-sess-reuse-1",
            iso_path="/tmp/fake.iso",
            pid=None,
            ws_url="ws://127.0.0.1:12345/debugger",
            created_at=datetime.now(timezone.utc),
            last_active_at=datetime.now(timezone.utc),
            exec_count=0,
            ws_connected=False,
        )
        sm_mod._save_sessions({"test-sess-reuse-1": sess})

        # Inject a FakeTransport + observer (simulating bind phase).
        transport = FakeTransport()
        transport.set_state({"stepping": False})
        transport.set_response("broadcast.config.set", {})
        observer = GameStateObserver(transport)
        manager._transports["test-sess-reuse-1"] = transport
        manager._observers["test-sess-reuse-1"] = observer

        # Track whether transport.close() is called (FakeTransport has no
        # close method — we add a spy to detect any close attempt).
        close_called = []
        transport.close = lambda: close_called.append(True)  # type: ignore[method-assign]

        # session_client_with_transport should reuse the injected transport.
        async with session_client_with_transport("test-sess-reuse-1") as (
            client,
            used_transport,
        ):
            # The transport returned is the same one injected.
            assert used_transport is transport, (
                "session_client_with_transport must reuse the session-level "
                "transport (accept phase) — not create a new one."
            )
            # PpssppDebugClient is constructed with the session-level transport.
            assert client._transport is transport

        # After exit: transport.close() must NOT have been called
        # (session stop owns the transport lifecycle, not per-call).
        assert close_called == [], (
            "session_client_with_transport must NOT close the transport "
            "on exit — SessionManager.stop_session owns the transport "
            "lifecycle (close phase, task 4.4)."
        )

    @pytest.mark.asyncio
    async def test_observer_forwarded_to_stepping_manager(
        self, isolated_sessions_path: Path, monkeypatch
    ):
        """13.8b: PpssppDebugClient is constructed with the session-level
        observer, which is forwarded to SteppingManager for resume()
        broadcast confirmation.
        """
        from ppsspp_dfx_mcp.models.session import Session
        from datetime import datetime, timezone

        monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "")

        # Use the singleton — same rationale as test_reuses_session_level_transport.
        manager = sm_mod.get_session_manager()
        sess = Session(
            session_id="test-sess-reuse-2",
            iso_path="/tmp/fake.iso",
            pid=None,
            ws_url="ws://127.0.0.1:12345/debugger",
            created_at=datetime.now(timezone.utc),
            last_active_at=datetime.now(timezone.utc),
            exec_count=0,
            ws_connected=False,
        )
        sm_mod._save_sessions({"test-sess-reuse-2": sess})

        transport = FakeTransport()
        transport.set_state({"stepping": False})
        observer = GameStateObserver(transport)
        manager._transports["test-sess-reuse-2"] = transport
        manager._observers["test-sess-reuse-2"] = observer

        async with session_client_with_transport("test-sess-reuse-2") as (
            client,
            _,
        ):
            # SteppingManager should have the observer injected.
            assert client._stepping._game_state_observer is observer, (
                "PpssppDebugClient must forward the session-level observer "
                "to SteppingManager for resume() broadcast confirmation."
            )
