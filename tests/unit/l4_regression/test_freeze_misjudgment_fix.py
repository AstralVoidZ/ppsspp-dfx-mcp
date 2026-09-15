"""L4 regression tests for fix-ppsspp-dfx-freeze-misjudgment (tasks 12.1-12.6).

Anchors the CPU freeze misjudgment fix that distinguishes:
- ``CpuFreezeSuspected`` — PPSSPP process alive but CPU not progressing
  (CPU death-loop / HLE block / GPU pipeline stall).
- ``WsDisconnected`` — PPSSPP process dead or WS connection lost.
- ``CpuStateError`` — caller misuse (ticketed call while game paused).

Test groups:
- 12.1: ``to_tool_error`` ``SteppingFailedError`` translation split into
  multiple paths (pid_alive=True/False/None + ConnectionError cause).
- 12.2: ``to_tool_error`` ``TimeoutError`` comprehensive judgment (PID +
  game state decision matrix).
- 12.3: ``_require_running`` ticks breakpoint pause detection (stepping
  field + ticks backup validation).
- 12.4: ``SteppingManager.pause()`` PID pre-check (pid_alive attribute
  propagation + non-TimeoutError passthrough).
- 12.5: ``SteppingManager.resume()`` broadcast confirmation path
  (observer True/False/None).
- 12.6: ``with_stepping`` avoid double-wrapping ``SteppingFailedError``
  (preserve pid_alive + __cause__ chain).

"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fake_transport import FakeTransport
from ppsspp_dfx_mcp.core.stepping import SteppingManager
from ppsspp_dfx_mcp.errors import (
    CpuFreezeSuspected,
    CpuStateError,
    SteppingFailedError,
    ToolError,
    WsDisconnected,
    WsTimeout,
    set_error_context,
    to_tool_error,
)
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


# ============================================================================
# 12.1 — to_tool_error SteppingFailedError translation split (multi-path)
# ============================================================================


class TestSteppingFailedErrorTranslation:
    """12.1: ``to_tool_error`` translates ``SteppingFailedError`` based on
    ``__cause__`` type + ``pid_alive`` attribute (two-signal judgment).

    Decision matrix (cpu-freeze-detection/spec.md):
    - ``__cause__`` is ``ConnectionError`` → ``WsDisconnected`` (regardless
      of ``pid_alive`` — transport-level failure wins).
    - ``pid_alive=True`` + ``TimeoutError`` cause → ``CpuFreezeSuspected``.
    - ``pid_alive=False`` + ``TimeoutError`` cause → ``WsDisconnected``.
    - ``pid_alive=None`` + ``TimeoutError`` cause → ``CpuFreezeSuspected``
      (default conservative — without PID context, treat timeout-during-
      pause as suspected freeze).
    """

    def test_pid_alive_true_timeout_cause_translates_to_cpu_freeze(self):
        """12.1a: SteppingFailedError(pid_alive=True) with TimeoutError
        cause → CpuFreezeSuspected.
        """
        try:
            raise TimeoutError("wait_for_state timed out")
        except TimeoutError as cause:
            exc = SteppingFailedError(
                "pause failed: PID alive but CPU not entering STEPPING",
                pid_alive=True,
            )
            exc.__cause__ = cause

        result = to_tool_error(exc)

        assert isinstance(result, CpuFreezeSuspected), (
            "pid_alive=True + TimeoutError cause must translate to "
            "CpuFreezeSuspected (PID alive but CPU not entering "
            "STEPPING — suspected freeze)."
        )
        assert result.code == "CPU_FREEZE_SUSPECTED"

    def test_pid_alive_false_timeout_cause_translates_to_ws_disconnected(self):
        """12.1b: SteppingFailedError(pid_alive=False) with TimeoutError
        cause → WsDisconnected (PID dead = real disconnect).
        """
        try:
            raise TimeoutError("wait_for_state timed out")
        except TimeoutError as cause:
            exc = SteppingFailedError(
                "pause failed: PID dead; PPSSPP process no longer running",
                pid_alive=False,
            )
            exc.__cause__ = cause

        result = to_tool_error(exc)

        assert isinstance(result, WsDisconnected), (
            "pid_alive=False + TimeoutError cause must translate to "
            "WsDisconnected (PID dead = real disconnect, not a freeze)."
        )
        assert result.code == "WS_DISCONNECTED"

    def test_pid_alive_none_timeout_cause_translates_to_cpu_freeze_default(self):
        """12.1c: SteppingFailedError(pid_alive=None) with TimeoutError
        cause → CpuFreezeSuspected (default conservative).

        Without PID context, treat timeout-during-pause as a suspected
        freeze (conservative — prefer diagnose-then-recover over restart).
        """
        try:
            raise TimeoutError("wait_for_state timed out")
        except TimeoutError as cause:
            exc = SteppingFailedError(
                "pause failed; CPU state unknown, cannot enter stepping context",
                pid_alive=None,
            )
            exc.__cause__ = cause

        result = to_tool_error(exc)

        assert isinstance(result, CpuFreezeSuspected), (
            "pid_alive=None + TimeoutError cause must translate to "
            "CpuFreezeSuspected as default conservative (no PID context "
            "to distinguish disconnect vs freeze — prefer freeze "
            "diagnosis path)."
        )

    def test_connection_refused_cause_translates_to_ws_disconnected_regardless_of_pid(self):
        """12.1d: SteppingFailedError with ConnectionRefusedError cause →
        WsDisconnected regardless of pid_alive (transport-level failure
        wins over PID signal).
        """
        # pid_alive=True is irrelevant — ConnectionError cause wins.
        try:
            raise ConnectionRefusedError("WebSocket not connected")
        except ConnectionRefusedError as cause:
            exc = SteppingFailedError(
                "pause failed; CPU state unknown", pid_alive=True
            )
            exc.__cause__ = cause

        result = to_tool_error(exc)

        assert isinstance(result, WsDisconnected), (
            "ConnectionError cause must translate to WsDisconnected "
            "regardless of pid_alive attribute — transport-level failure "
            "overrides the PID signal."
        )


# ============================================================================
# 12.2 — to_tool_error TimeoutError comprehensive judgment
# ============================================================================


class TestTimeoutErrorComprehensiveJudgment:
    """12.2: ``to_tool_error`` translates ``TimeoutError`` via PID + game
    state综合判定 (decision 9, cpu-freeze-detection/spec.md).

    Decision matrix:
    - PID dead → WsDisconnected (real disconnect).
    - PID alive + game ``running`` → CpuFreezeSuspected (suspected freeze).
    - PID alive + game ``paused`` → CpuStateError (caller misuse).
    - PID alive + game ``quit`` → WsDisconnected (game exited).
    - PID alive + game ``loading`` → WsTimeout (preserve existing behavior).
    - PID alive + game state unknown (observer None) → WsTimeout (conservative).
    - PID context not available (no PID) → WsTimeout (preserve existing).
    """

    def setup_method(self):
        """Reset error context before each test (isolation)."""
        set_error_context(None, None)
        self._orig_is_pid_alive = None

    def teardown_method(self):
        """Restore clean state after each test (context AND proc patch)."""
        set_error_context(None, None)
        if self._orig_is_pid_alive is not None:
            import ppsspp_dfx_mcp.core.proc as proc_mod
            proc_mod.is_pid_alive = self._orig_is_pid_alive
            self._orig_is_pid_alive = None

    def _set_context(self, pid_alive: bool | None, game_state: str | None) -> None:
        """Inject PID + game-state resolvers returning the given values."""
        set_error_context(
            pid_resolver=lambda: 12345 if pid_alive is not None else None,
            game_state_resolver=lambda: game_state,
        )
        # Patch is_pid_alive to return the requested pid_alive value.
        # _resolve_pid_alive calls proc.is_pid_alive(pid) — patch the
        # core.proc module attribute.
        from ppsspp_dfx_mcp.errors import _resolve_pid_alive
        # Inject by patching the function lookup in the errors module. We
        # achieve this by overriding _pid_resolver to also drive the
        # alive result, and patching is_pid_alive in core.proc.
        import ppsspp_dfx_mcp.core.proc as proc_mod
        if self._orig_is_pid_alive is None:
            # Save once per test — a second _set_context call in the same
            # test must not snapshot the lambda itself (restore-the-patch
            # bug: the lambda would leak into every later test).
            self._orig_is_pid_alive = proc_mod.is_pid_alive
        if pid_alive is None:
            # _resolve_pid_alive returns None when pid_resolver returns None.
            set_error_context(
                pid_resolver=lambda: None,
                game_state_resolver=lambda: game_state,
            )
        else:
            set_error_context(
                pid_resolver=lambda: 12345,
                game_state_resolver=lambda: game_state,
            )
            proc_mod.is_pid_alive = lambda _pid: pid_alive

    def test_pid_dead_translates_to_ws_disconnected(self):
        """12.2a: TimeoutError + PID dead → WsDisconnected (real disconnect)."""
        self._set_context(pid_alive=False, game_state="running")
        exc = asyncio.TimeoutError("gpu.stats.get timed out")

        result = to_tool_error(exc)

        assert isinstance(result, WsDisconnected), (
            "TimeoutError + PID dead must translate to WsDisconnected "
            "(real disconnect — PPSSPP process no longer running)."
        )

    def test_pid_alive_game_running_translates_to_cpu_freeze(self):
        """12.2b: TimeoutError + PID alive + game running → CpuFreezeSuspected."""
        self._set_context(pid_alive=True, game_state="running")
        exc = asyncio.TimeoutError("gpu.stats.get timed out")

        result = to_tool_error(exc)

        assert isinstance(result, CpuFreezeSuspected), (
            "TimeoutError + PID alive + game running must translate to "
            "CpuFreezeSuspected (suspected freeze: death-loop / HLE "
            "block / GPU pipeline stall)."
        )

    def test_pid_alive_game_paused_translates_to_cpu_state_error(self):
        """12.2c: TimeoutError + PID alive + game paused → CpuStateError
        (caller misuse: ticketed call should not be invoked while paused).
        """
        self._set_context(pid_alive=True, game_state="paused")
        exc = asyncio.TimeoutError("gpu.stats.get timed out")

        result = to_tool_error(exc)

        assert isinstance(result, CpuStateError), (
            "TimeoutError + PID alive + game paused must translate to "
            "CpuStateError (caller misuse — ticketed call should not "
            "be invoked while paused)."
        )

    def test_pid_alive_game_quit_translates_to_ws_disconnected(self):
        """12.2d: TimeoutError + PID alive + game quit → WsDisconnected
        (game exited, session should be restarted).
        """
        self._set_context(pid_alive=True, game_state="quit")
        exc = asyncio.TimeoutError("gpu.stats.get timed out")

        result = to_tool_error(exc)

        assert isinstance(result, WsDisconnected), (
            "TimeoutError + PID alive + game quit must translate to "
            "WsDisconnected (game exited — session is dead)."
        )

    def test_pid_alive_game_loading_translates_to_ws_timeout(self):
        """12.2e: TimeoutError + PID alive + game loading → WsTimeout
        (large file load may be slow — preserve existing behavior).
        """
        self._set_context(pid_alive=True, game_state="loading")
        exc = asyncio.TimeoutError("gpu.stats.get timed out")

        result = to_tool_error(exc)

        assert isinstance(result, WsTimeout), (
            "TimeoutError + PID alive + game loading must translate to "
            "WsTimeout (large file load may be slow — preserve existing "
            "behavior)."
        )

    def test_pid_alive_game_state_unknown_translates_to_ws_timeout(self):
        """12.2f: TimeoutError + PID alive + game state unknown (observer
        not available) → WsTimeout (conservative — preserve existing).
        """
        self._set_context(pid_alive=True, game_state=None)
        exc = asyncio.TimeoutError("gpu.stats.get timed out")

        result = to_tool_error(exc)

        assert isinstance(result, WsTimeout), (
            "TimeoutError + PID alive + game state unknown (observer "
            "not configured) must translate to WsTimeout (conservative "
            "default — preserve existing behavior)."
        )

    def test_pid_context_not_available_translates_to_ws_timeout(self):
        """12.2g: TimeoutError + PID context not available → WsTimeout
        (preserve existing behavior, conservative default).
        """
        self._set_context(pid_alive=None, game_state=None)
        exc = asyncio.TimeoutError("gpu.stats.get timed out")

        result = to_tool_error(exc)

        assert isinstance(result, WsTimeout), (
            "TimeoutError without PID context must translate to "
            "WsTimeout (preserve existing behavior)."
        )


# ============================================================================
# 12.3 — _require_running ticks breakpoint pause detection
# ============================================================================


class TestRequireRunningTicksDetection:
    """12.3: ``_require_running`` ticks断点暂停识别 (decision 4, re-scoped
    to breakpoint pause identification only — NOT for CPU death-loop/HLE
    block detection which is handled by to_tool_error TimeoutError path).

    Decision matrix (cpu-freeze-detection/spec.md):
    - stepping=True (either probe) → CpuStateError (existing behavior).
    - stepping=False (both probes) AND ticks0==ticks1 (both present) →
      CpuStateError (ticks backup validation: breakpoint pause suspected
      but stepping field didn't report).
    - stepping=False (both probes) AND ticks0!=ticks1 → return (CPU
      progressing normally, allow ticketed call to proceed).
    """

    @pytest.mark.asyncio
    async def test_stepping_true_raises_cpu_state_error(self):
        """12.3a: stepping=True at first probe → CpuStateError."""
        transport = FakeTransport()
        transport.set_state({"stepping": True, "ticks": 100})
        client = PpssppDebugClient(transport)

        with pytest.raises(CpuStateError, match="requires CPU running"):
            await client._require_running("gpu.stats.get")

    @pytest.mark.asyncio
    async def test_ticks_unchanged_raises_cpu_state_error(self):
        """12.3b: stepping=False (both probes) + ticks0==ticks1 →
        CpuStateError (ticks backup validation).
        """
        transport = FakeTransport()
        # ticks stays at 100 across both probes (FakeTransport returns
        # the same state dict until set_state is called).
        transport.set_state({"stepping": False, "ticks": 100})
        client = PpssppDebugClient(transport)

        with pytest.raises(CpuStateError, match="ticks unchanged"):
            await client._require_running("gpu.stats.get")

    @pytest.mark.asyncio
    async def test_ticks_progressing_does_not_raise(self):
        """12.3c: stepping=False (both probes) + ticks0!=ticks1 → no
        exception (CPU progressing normally).

        Simulates a running CPU: the second cpu.status returns a higher
        ticks value (background counter advancing).
        """
        transport = FakeTransport()
        transport.set_state({"stepping": False, "ticks": 100})

        # Configure cpu.status to return increasing ticks on each call.
        call_count = {"n": 0}

        def _status_response(**params: Any) -> dict[str, Any]:
            call_count["n"] += 1
            return {"stepping": False, "ticks": 100 + call_count["n"]}

        # Override the call() method to return the dynamic ticks.
        original_call = transport.call

        async def _patched_call(event: str, timeout: float = 5.0, **params: Any):
            if event == "cpu.status":
                return _status_response(**params)
            return await original_call(event, timeout=timeout, **params)

        transport.call = _patched_call  # type: ignore[assignment]
        client = PpssppDebugClient(transport)

        # Should NOT raise — ticks0=101, ticks1=102 (after 50ms sleep).
        await client._require_running("gpu.stats.get")


# ============================================================================
# 12.4 — SteppingManager.pause() PID pre-check
# ============================================================================


class TestPausePidPreCheck:
    """12.4: ``SteppingManager.pause()`` PID pre-check (decision 4).

    On ``wait_for_state(stepping=True)`` timeout, ``pause()`` performs a
    PID-alive pre-check (if ``self._pid`` is set) and constructs a
    ``SteppingFailedError`` with the ``pid_alive`` attribute set.

    Tests:
    - ``pid=None`` + TimeoutError → SteppingFailedError without pid_alive.
    - ``pid=valid`` + TimeoutError + PID alive → SteppingFailedError(pid_alive=True).
    - ``pid=invalid`` + TimeoutError + PID dead → SteppingFailedError(pid_alive=False).
    - non-TimeoutError exception propagates (not caught by pause()).
    """

    @pytest.mark.asyncio
    async def test_pid_none_timeout_raises_without_pid_alive(self):
        """12.4a: pid=None + TimeoutError → SteppingFailedError without
        pid_alive (defaults to None).
        """
        transport = FakeTransport()
        transport.set_state({"stepping": False})

        # Configure wait_for_state to always time out (predicate never
        # satisfied — stepping stays False after fire_and_forget).
        # Override wait_for_state to raise TimeoutError directly.
        async def _timeout_wait(predicate: Any, timeout_ms: int = 3000, interval_ms: int = 50) -> dict[str, Any]:
            raise TimeoutError("wait_for_state timed out")

        transport.wait_for_state = _timeout_wait  # type: ignore[assignment]

        mgr = SteppingManager(transport, pid=None)

        with pytest.raises(SteppingFailedError) as exc_info:
            await mgr.pause()

        assert exc_info.value.pid_alive is None, (
            "pause() with pid=None must raise SteppingFailedError "
            "without pid_alive attribute (defaults to None)."
        )
        assert isinstance(exc_info.value.__cause__, TimeoutError), (
            "SteppingFailedError.__cause__ must be the original TimeoutError."
        )

    @pytest.mark.asyncio
    async def test_pid_alive_timeout_raises_with_pid_alive_true(self, monkeypatch):
        """12.4b: pid=valid + TimeoutError + PID alive →
        SteppingFailedError(pid_alive=True).
        """
        transport = FakeTransport()
        transport.set_state({"stepping": False, "ticks": 12345})

        async def _timeout_wait(predicate: Any, timeout_ms: int = 3000, interval_ms: int = 50) -> dict[str, Any]:
            raise TimeoutError("wait_for_state timed out")

        transport.wait_for_state = _timeout_wait  # type: ignore[assignment]

        # Patch is_pid_alive to return True (PID alive).
        import ppsspp_dfx_mcp.core.proc as sm_mod
        monkeypatch.setattr(sm_mod, "is_pid_alive", lambda _pid: True)

        mgr = SteppingManager(transport, pid=99999)

        with pytest.raises(SteppingFailedError) as exc_info:
            await mgr.pause()

        assert exc_info.value.pid_alive is True, (
            "pause() with valid PID + PID alive must raise "
            "SteppingFailedError(pid_alive=True)."
        )
        assert "PID alive" in str(exc_info.value), (
            "Error message must encode PID alive status."
        )
        assert isinstance(exc_info.value.__cause__, TimeoutError)

    @pytest.mark.asyncio
    async def test_pid_dead_timeout_raises_with_pid_alive_false(self, monkeypatch):
        """12.4c: pid=invalid + TimeoutError + PID dead →
        SteppingFailedError(pid_alive=False).
        """
        transport = FakeTransport()
        transport.set_state({"stepping": False, "ticks": 12345})

        async def _timeout_wait(predicate: Any, timeout_ms: int = 3000, interval_ms: int = 50) -> dict[str, Any]:
            raise TimeoutError("wait_for_state timed out")

        transport.wait_for_state = _timeout_wait  # type: ignore[assignment]

        # Patch is_pid_alive to return False (PID dead).
        import ppsspp_dfx_mcp.core.proc as sm_mod
        monkeypatch.setattr(sm_mod, "is_pid_alive", lambda _pid: False)

        mgr = SteppingManager(transport, pid=99999)

        with pytest.raises(SteppingFailedError) as exc_info:
            await mgr.pause()

        assert exc_info.value.pid_alive is False, (
            "pause() with valid PID + PID dead must raise "
            "SteppingFailedError(pid_alive=False)."
        )
        assert "PID dead" in str(exc_info.value), (
            "Error message must encode PID dead status."
        )
        assert isinstance(exc_info.value.__cause__, TimeoutError)

    @pytest.mark.asyncio
    async def test_non_timeout_exception_propagates_unchanged(self):
        """12.4d: non-TimeoutError exception (e.g. ConnectionRefusedError)
        propagates unchanged from pause() — pause() only catches
        TimeoutError.
        """
        transport = FakeTransport()
        transport.set_state({"stepping": False})

        # Configure wait_for_state to raise ConnectionRefusedError (not
        # TimeoutError). pause() must NOT catch this — it propagates to
        # with_stepping which wraps it in SteppingFailedError.
        async def _conn_refused_wait(predicate: Any, timeout_ms: int = 3000, interval_ms: int = 50) -> dict[str, Any]:
            raise ConnectionRefusedError("WS not connected")

        transport.wait_for_state = _conn_refused_wait  # type: ignore[assignment]

        mgr = SteppingManager(transport, pid=99999)

        # pause() should propagate ConnectionRefusedError directly.
        with pytest.raises(ConnectionRefusedError):
            await mgr.pause()


# ============================================================================
# 12.5 — SteppingManager.resume() broadcast confirmation path
# ============================================================================


class TestResumeBroadcastConfirmation:
    """12.5: ``SteppingManager.resume()`` broadcast confirmation path
    (decision 8, stepping-manager/spec.md).

    Decision matrix:
    - observer configured + ``wait_for_resume`` returns True → return
      immediately (no polling cpu.status).
    - observer configured + ``wait_for_resume`` returns False → fall
      back to ``wait_for_state(stepping=False)`` polling.
    - observer is None → existing ``wait_for_state`` behavior unchanged.
    """

    @pytest.mark.asyncio
    async def test_observer_returns_true_skips_polling(self):
        """12.5a: observer configured + wait_for_resume returns True →
        resume() returns without polling wait_for_state.
        """
        transport = FakeTransport()
        transport.set_state({"stepping": True})

        # observer mock — wait_for_resume returns True.
        observer = AsyncMock()
        observer.wait_for_resume.return_value = True

        mgr = SteppingManager(transport, game_state_observer=observer)

        # Configure wait_for_state to raise if called (should NOT be).
        async def _should_not_be_called(predicate: Any, timeout_ms: int = 3000, interval_ms: int = 50) -> dict[str, Any]:
            raise AssertionError("wait_for_state should not be called when broadcast confirms resume")

        transport.wait_for_state = _should_not_be_called  # type: ignore[assignment]

        result = await mgr.resume()

        # wait_for_resume was awaited once with the default 3000ms timeout.
        observer.wait_for_resume.assert_awaited_once_with(timeout_ms=3000)
        # Result is empty dict (no polling — broadcast confirmed).
        assert result == {}

    @pytest.mark.asyncio
    async def test_observer_returns_false_falls_back_to_polling(self):
        """12.5b: observer configured + wait_for_resume returns False →
        fall back to wait_for_state(stepping=False) polling.
        """
        transport = FakeTransport()
        # stepping=True initially — wait_for_state's predicate (stepping
        # is False) is NOT satisfied. Use a faf handler to flip stepping
        # to False on cpu.resume, so the predicate eventually matches.
        transport.set_state({"stepping": True})

        def _set_stepping_false(t: FakeTransport, **params: Any) -> None:
            cur = t.state
            t.set_state({**cur, "stepping": False})

        transport.set_faf_handler("cpu.resume", _set_stepping_false)

        # observer mock — wait_for_resume returns False (broadcast timeout).
        observer = AsyncMock()
        observer.wait_for_resume.return_value = False

        mgr = SteppingManager(transport, game_state_observer=observer)

        result = await mgr.resume()

        # wait_for_resume was awaited.
        observer.wait_for_resume.assert_awaited_once_with(timeout_ms=3000)
        # wait_for_state was called as fallback — result has stepping=False.
        assert result.get("stepping") is False

    @pytest.mark.asyncio
    async def test_observer_none_uses_polling_only(self):
        """12.5c: observer=None → existing wait_for_state behavior
        unchanged (no broadcast confirmation attempted).
        """
        transport = FakeTransport()
        transport.set_state({"stepping": True})

        def _set_stepping_false(t: FakeTransport, **params: Any) -> None:
            cur = t.state
            t.set_state({**cur, "stepping": False})

        transport.set_faf_handler("cpu.resume", _set_stepping_false)

        mgr = SteppingManager(transport, game_state_observer=None)

        result = await mgr.resume()

        # No observer → no wait_for_resume call.
        # Result has stepping=False (polling confirmed resume).
        assert result.get("stepping") is False


# ============================================================================
# 12.6 — with_stepping avoid double-wrapping SteppingFailedError
# ============================================================================


class TestWithSteppingNoDoubleWrap:
    """12.6: ``with_stepping`` avoids double-wrapping ``SteppingFailedError``
    (stepping-manager/spec.md).

    Decision matrix:
    - ``pause()`` raises ``SteppingFailedError`` → ``with_stepping``
      propagates it directly (preserves ``pid_alive`` + ``__cause__``).
    - ``pause()`` raises non-``SteppingFailedError`` (e.g.
      ``ConnectionRefusedError``) → ``with_stepping`` wraps it in
      ``SteppingFailedError(...) from e`` (existing behavior).
    """

    @pytest.mark.asyncio
    async def test_stepping_failed_error_propagates_directly(self, monkeypatch):
        """12.6a: pause() raises SteppingFailedError(pid_alive=True) →
        with_stepping propagates it directly without re-wrapping.

        Without the fix, with_stepping would wrap the SteppingFailedError
        in another SteppingFailedError, losing the pid_alive attribute
        and masking the original __cause__ type.
        """
        transport = FakeTransport()
        transport.set_state({"stepping": False, "ticks": 100})

        # Configure wait_for_state to time out (so pause() takes the
        # PID pre-check path).
        async def _timeout_wait(predicate: Any, timeout_ms: int = 3000, interval_ms: int = 50) -> dict[str, Any]:
            raise TimeoutError("wait_for_state timed out")

        transport.wait_for_state = _timeout_wait  # type: ignore[assignment]

        # Patch is_pid_alive to return True.
        import ppsspp_dfx_mcp.core.proc as sm_mod
        monkeypatch.setattr(sm_mod, "is_pid_alive", lambda _pid: True)

        mgr = SteppingManager(transport, pid=99999)

        with pytest.raises(SteppingFailedError) as exc_info:
            async with mgr.with_stepping():
                # Body should never execute — pause() raises before yield.
                pytest.fail("body should not execute when pause() fails")

        # The propagated exception must retain pid_alive=True (not
        # double-wrapped, which would lose the attribute).
        assert exc_info.value.pid_alive is True, (
            "with_stepping must propagate SteppingFailedError directly "
            "without double-wrapping — pid_alive attribute must be "
            "preserved."
        )
        # __cause__ chain must be intact (TimeoutError from pause()).
        assert isinstance(exc_info.value.__cause__, TimeoutError), (
            "__cause__ chain must be preserved (TimeoutError from pause())."
        )

    @pytest.mark.asyncio
    async def test_non_stepping_failed_error_wrapped(self):
        """12.6b: pause() raises ConnectionRefusedError (not
        SteppingFailedError, because pause() only catches TimeoutError)
        → with_stepping wraps it in SteppingFailedError(...) from e.
        """
        transport = FakeTransport()
        transport.set_state({"stepping": False})

        # Configure wait_for_state to raise ConnectionRefusedError.
        async def _conn_refused_wait(predicate: Any, timeout_ms: int = 3000, interval_ms: int = 50) -> dict[str, Any]:
            raise ConnectionRefusedError("WS not connected")

        transport.wait_for_state = _conn_refused_wait  # type: ignore[assignment]

        mgr = SteppingManager(transport, pid=99999)

        with pytest.raises(SteppingFailedError) as exc_info:
            async with mgr.with_stepping():
                # Body should never execute — pause() raises before yield.
                pytest.fail("body should not execute when pause() fails")

        # The wrapped exception must have __cause__ set to the original
        # ConnectionRefusedError (existing behavior for non-timeout
        # exceptions).
        assert isinstance(exc_info.value.__cause__, ConnectionRefusedError), (
            "with_stepping must wrap non-SteppingFailedError exceptions "
            "in SteppingFailedError(...) from e (existing behavior)."
        )
        # pid_alive is None (default) — pause() did not perform PID
        # pre-check (only triggered on TimeoutError).
        assert exc_info.value.pid_alive is None
