"""G-7 (FR-007): "running but not rendering" must not read as a CPU freeze.

`to_tool_error` derives CPU_FREEZE_SUSPECTED from "PID alive + game state
running" (errors.py `_translate_timeout_error`). Neither half says anything
about the GPU: a game rendering nothing at all — black screen, a modal
"Graphics Error" dialog, a GPU pipeline stall — still reports `running`,
because the CPU genuinely is executing.

So `ppsspp_gpu_stats` on such a session reported CPU_FREEZE_SUSPECTED and
sent the caller after a CPU death-loop / HLE block, which by construction
cannot be the cause: the measured fact was "no frames are arriving". That
misdirection is the defect (deep-test report ppsspp_gpu_stats.md, G-7).

Fix: the game-state observer already keeps a frame heartbeat
(`_last_frame_mono`, refreshed by the `gpu.stats.get` consumer, already used
by the freeze detector). `GameStateObserver.is_rendering()` exposes it, and
the gpu_stats tool re-checks a CPU_FREEZE_SUSPECTED against it before
passing the code on.

Truth table (spec.md FR-007) — all three directions asserted:

  running + frames arriving    -> CPU_FREEZE_SUSPECTED stands (a real freeze)
  running + NO frames arriving -> NOT CPU_FREEZE_SUSPECTED; WS_TIMEOUT with
                                 a no-producer attribution
  paused                      -> CPU_STATE_ERROR, untouched (the caller
                                 misused the tool, not a hardware fault)
  undeterminable (no observer) -> CPU_FREEZE_SUSPECTED stands, unchanged
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from ppsspp_dfx_mcp.errors import CpuFreezeSuspected, ToolError, WsTimeout
from ppsspp_dfx_mcp.tools import gpu_stats as gpu_stats_mod
from ppsspp_dfx_mcp.tools.gpu_stats import gpu_stats


class _FakeObserver:
    """Stands in for GameStateObserver; `rendering` is the tri-state answer."""

    def __init__(self, rendering: bool | None) -> None:
        self._rendering = rendering
        self.calls = 0

    def is_rendering(self, max_stale_s: float = 3.0) -> bool | None:
        self.calls += 1
        return self._rendering

    def get_state(self) -> str:
        return "running"


class _FakeClient:
    """A client whose gpu_stats call fails with the given ToolError."""

    def __init__(self, exc: Exception, observer: _FakeObserver | None) -> None:
        self._exc = exc
        self._game_state_observer = observer

    async def gpu_stats(self) -> dict:
        raise self._exc


def _freeze_error() -> CpuFreezeSuspected:
    """The error `to_tool_error` produces for PID alive + game running."""
    return CpuFreezeSuspected("timed out — Hint: PID alive + game running")


def _patch(monkeypatch: pytest.MonkeyPatch, exc: Exception, observer) -> None:
    client = _FakeClient(exc, observer)

    @asynccontextmanager
    async def fake_session_client(_session_id: str) -> AsyncIterator[_FakeClient]:
        yield client

    monkeypatch.setattr(gpu_stats_mod, "session_client", fake_session_client)
    monkeypatch.setattr(gpu_stats_mod, "require_session_id", lambda _sid: _sid)


class TestTruthTableRunningWithoutFrames:
    """(running + no frames) MUST NOT be reported as a CPU freeze."""

    async def test_no_frames_is_not_reported_as_cpu_freeze(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(monkeypatch, _freeze_error(), _FakeObserver(rendering=False))
        with pytest.raises(ToolError) as exc_info:
            await gpu_stats(session_id="s")
        assert not isinstance(exc_info.value, CpuFreezeSuspected), (
            "a session producing no frames must not be reported as a CPU "
            "freeze — the CPU may well be executing fine (G-7)"
        )

    async def test_no_frames_reports_ws_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch(monkeypatch, _freeze_error(), _FakeObserver(rendering=False))
        with pytest.raises(WsTimeout):
            await gpu_stats(session_id="s")

    async def test_no_frames_message_names_the_renderer_not_the_cpu(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The replacement message must point at the GPU side, otherwise the
        caller repeats the same CPU investigation."""
        _patch(monkeypatch, _freeze_error(), _FakeObserver(rendering=False))
        with pytest.raises(ToolError) as exc_info:
            await gpu_stats(session_id="s")
        message = str(exc_info.value)
        assert "NOT a CPU freeze" in message, message
        assert "renderer" in message, message


class TestTruthTableRunningWithFrames:
    """(running + frames arriving) — the CPU freeze code must STAND.

    Two-way guard: without it, the fix would swallow every genuine freeze.
    """

    async def test_frames_arriving_keeps_cpu_freeze_suspected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch(monkeypatch, _freeze_error(), _FakeObserver(rendering=True))
        with pytest.raises(CpuFreezeSuspected):
            await gpu_stats(session_id="s")

    async def test_rendering_is_actually_consulted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fails if the heartbeat check is dead code — the observer is asked."""
        observer = _FakeObserver(rendering=True)
        _patch(monkeypatch, _freeze_error(), observer)
        with pytest.raises(CpuFreezeSuspected):
            await gpu_stats(session_id="s")
        assert observer.calls > 0, "the frame heartbeat was never queried"


class TestTruthTableUndeterminable:
    """Unknown rendering MUST NOT be read as "not rendering".

    Relabelling an ordinary CPU freeze as a GPU fault would be a worse
    misdiagnosis than the original one.
    """

    @pytest.mark.parametrize("observer", [None, _FakeObserver(rendering=None)])
    async def test_unknown_rendering_keeps_cpu_freeze_suspected(
        self, monkeypatch: pytest.MonkeyPatch, observer
    ) -> None:
        _patch(monkeypatch, _freeze_error(), observer)
        with pytest.raises(CpuFreezeSuspected):
            await gpu_stats(session_id="s")

    async def test_client_without_observer_attribute_is_tolerated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A client object without the attribute must not crash the handler."""

        class _Bare:
            async def gpu_stats(self) -> dict:
                raise _freeze_error()

        @asynccontextmanager
        async def fake_session_client(_session_id: str) -> AsyncIterator[_Bare]:
            yield _Bare()

        monkeypatch.setattr(gpu_stats_mod, "session_client", fake_session_client)
        monkeypatch.setattr(gpu_stats_mod, "require_session_id", lambda _sid: _sid)
        with pytest.raises(CpuFreezeSuspected):
            await gpu_stats(session_id="s")


class TestTruthTablePausedIsUntouched:
    """(paused) → CPU_STATE_ERROR. This tool never saw that path, so the
    fix must not change how a paused session is reported."""

    async def test_paused_cpu_state_error_is_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ppsspp_dfx_mcp.errors import CpuStateError

        _patch(monkeypatch, CpuStateError("game paused"), _FakeObserver(rendering=False))
        with pytest.raises(CpuStateError):
            await gpu_stats(session_id="s")


class TestObserverIsRenderingContract:
    """The observer predicate itself — the three-state contract."""

    def test_reports_none_when_feed_never_started(self) -> None:
        from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver

        observer = GameStateObserver.__new__(GameStateObserver)
        observer._gpu_stats_feed_enabled = False
        observer._last_frame_mono = None
        assert observer.is_rendering() is None

    def test_reports_false_when_no_frame_arrived(self, monkeypatch) -> None:
        import time as _time

        from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver

        observer = GameStateObserver.__new__(GameStateObserver)
        observer._gpu_stats_feed_enabled = True
        observer._last_frame_mono = _time.monotonic() - 10.0
        assert observer.is_rendering() is False

    def test_reports_true_when_frame_is_fresh(self) -> None:
        import time as _time

        from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver

        observer = GameStateObserver.__new__(GameStateObserver)
        observer._gpu_stats_feed_enabled = True
        observer._last_frame_mono = _time.monotonic()
        assert observer.is_rendering() is True
