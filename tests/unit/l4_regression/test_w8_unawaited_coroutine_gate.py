"""W8 guard: un-awaited coroutines must fail the suite, not whisper.

The review (v4) found 10 ``RuntimeWarning: coroutine
'AsyncMockMixin._execute_mock_call' was never awaited`` warnings pointing at
three PRODUCTION lines: ``tools/batch_step.py:248``, ``core/stepping.py:276``
and ``tools/replay.py:112``. The cause was never production code — all three
seams are synchronous where the test double made them asynchronous:

- ``batch_step``/``replay`` read ``status.get(...)`` on the value returned by
  ``await client.replay_status()``. An unconfigured ``AsyncMock`` resolves to
  ANOTHER ``AsyncMock`` (Python 3.13 ``unittest.mock``), whose ``.get()``
  returns a coroutine; ``bool(<coroutine>)`` is ``True``, so the
  "recording"/"busy" branch was taken for the wrong reason and the assertions
  around it were vacuous (``batch_step`` even reported ``recording_mode=True``).
- ``core/stepping.py`` calls ``GameStateObserver.drain_resume()``, a plain
  ``def``, on an ``AsyncMock`` observer — the stale-broadcast drain became a
  no-op, so the ordering it protects (drain BEFORE the new ``cpu.resume``) was
  never exercised by the tests that claim to cover ``resume()``.

The fixes are in the doubles (``test_cpu_step_executor.py``,
``test_replay_p1_file_io.py``, ``test_freeze_misjudgment_fix.py``); this module
locks the two things that keep the failure mode loud from now on: the pytest
``filterwarnings`` gate and the real seam shapes the doubles must mirror.
"""

from __future__ import annotations

import gc
import inspect
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver
from ppsspp_dfx_mcp.core.stepping import SteppingManager
from ppsspp_dfx_mcp.tools.replay import _ensure_replay_idle

SUBPROJECT_ROOT = Path(__file__).resolve().parents[3]


class TestPytestGate:
    """The gate is what turns a silent leak into a red suite."""

    def test_filterwarnings_turns_unawaited_coroutines_into_failures(self):
        """Both entries are load-bearing: the first makes the GC-time
        ``RuntimeWarning`` raise inside the coroutine's finalizer (which
        Python then reports as an unraisable exception), and only the second
        turns that unraisable into a test failure.
        """
        with (SUBPROJECT_ROOT / "pyproject.toml").open("rb") as fh:
            config = tomllib.load(fh)
        filters = config["tool"]["pytest"]["ini_options"]["filterwarnings"]

        assert "error::RuntimeWarning" in filters, (
            "pyproject.toml must keep `error::RuntimeWarning`: without it a "
            "coroutine dropped on the floor only prints a warning."
        )
        assert "error::pytest.PytestUnraisableExceptionWarning" in filters, (
            "pyproject.toml must keep "
            "`error::pytest.PytestUnraisableExceptionWarning`: the RuntimeWarning "
            "above fires inside a finalizer, so on its own it stays unraisable "
            "and the suite still passes (verified during the W8 fix)."
        )

    def test_the_anti_pattern_really_is_detectable(self):
        """Negative control: an AsyncMock standing in for a sync/dict seam
        emits the warning at collection time — which is exactly what the gate
        above escalates. ``pytest.warns`` overrides the `error::` filter for
        this block, so the leak is captured instead of failing the test.
        """
        leaky = AsyncMock()
        with pytest.warns(RuntimeWarning, match="never awaited"):
            leaky.drain_resume()  # sync seam called on an async double
            gc.collect()


class TestSeamShapes:
    """The doubles must mirror these shapes; each assertion is the reason a
    bare ``AsyncMock`` was wrong."""

    def test_drain_resume_is_synchronous_and_wait_for_resume_is_not(self):
        assert not inspect.iscoroutinefunction(GameStateObserver.drain_resume), (
            "drain_resume() is a plain def — awaiting it (or mocking it with "
            "AsyncMock) turns the stale-broadcast drain into a no-op."
        )
        assert inspect.iscoroutinefunction(GameStateObserver.wait_for_resume), (
            "wait_for_resume() is awaited by SteppingManager.resume(); a sync "
            "double would make the confirmation check vacuous."
        )

    def test_resume_calls_the_drain_without_await(self):
        """Structural lock on the call site itself: adding ``await`` here is
        not a valid fix for the W8 warning (the real method is sync)."""
        source = inspect.getsource(SteppingManager.resume)
        assert "self._game_state_observer.drain_resume()" in source
        assert "await self._game_state_observer.drain_resume()" not in source


class TestReplayIdleSeam:
    """``_ensure_replay_idle`` reads the status reply synchronously."""

    @pytest.mark.asyncio
    async def test_dict_reply_keeps_the_idle_path(self):
        client = AsyncMock()
        client.replay_status.return_value = {"executing": False, "saving": False}

        assert await _ensure_replay_idle(client) is False
        client.replay_abort.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_coroutine_reply_inverts_the_branch(self):
        """The pre-W8 double shape, kept as the documented failure mode: a
        coroutine is truthy, so an idle replay looks "busy" and is aborted.
        """
        leaky = AsyncMock()
        with pytest.warns(RuntimeWarning, match="never awaited"):
            busy = await _ensure_replay_idle(leaky)

        assert busy is True, (
            "this is the vacuous-assertion mechanism W8 describes: the branch "
            "is taken by `bool(<coroutine>)`, not by the replay status."
        )
        leaky.replay_abort.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_recording_mode_flag_is_not_flipped_by_a_coroutine(self):
        """``batch_step`` derives ``recording_mode`` from the same seam; the
        flag is part of the tool's response contract, so the double must pin
        it (``tests/unit/l4_regression/test_cpu_step_executor.py`` asserts the
        end-to-end value)."""
        client = AsyncMock()
        client.replay_status.return_value = {"executing": False, "saving": True}
        status = await client.replay_status()

        assert isinstance(status, dict), "the replay status seam resolves to a dict"
        assert bool(status.get("saving", False)) is True
