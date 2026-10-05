"""Review-v4 W-7: awaiting a task we just cancel()ed must swallow only the
CHILD's cancellation, never an outer cancellation of the awaiting coroutine.

`except asyncio.CancelledError: pass` cannot tell the two apart: an outer
cancel arriving during the await is swallowed too, so the caller's task
refuses to die and cancel-scope accounting is corrupted. The fix routes
both observer cleanup loops through `core.task_cleanup.await_cancelled`,
which re-raises when the child is NOT what ended cancelled.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from ppsspp_dfx_mcp.core.task_cleanup import await_cancelled


async def _stubborn_child() -> str:
    """Cancelled but slow to notice: returns normally after a delay."""
    try:
        await asyncio.sleep(10)
    except asyncio.CancelledError:
        await asyncio.sleep(0.05)  # teardown after cancel request
        return "done"


@pytest.mark.asyncio
async def test_child_cancellation_is_swallowed():
    child = asyncio.create_task(_stubborn_child())
    child.cancel()
    await asyncio.wait_for(await_cancelled(child), timeout=2.0)


@pytest.mark.asyncio
async def test_child_exception_propagates():
    async def _boom() -> None:
        raise ValueError("child blew up")

    child = asyncio.create_task(_boom())
    with pytest.raises(ValueError):
        await await_cancelled(child)
    await asyncio.gather(child, return_exceptions=True)


@pytest.mark.asyncio
async def test_outer_cancellation_propagates():
    """The bug shape: cancel the WAITER while it awaits the child.

    Against the old inline pattern (`except CancelledError: pass`) this
    returned normally — the outer cancellation vanished.

    (The child here is deliberately still RUNNING, not pre-cancelled: on
    Python 3.13 a pre-cancelled child ends cancelled almost immediately —
    the cancellation is re-delivered past a bare `except` — so the waiter
    would finish before the outer cancel could land, making the scenario
    untestably racy. The two behavioral branches are covered separately.)
    """
    child = asyncio.create_task(asyncio.sleep(10))
    waiter = asyncio.create_task(await_cancelled(child))
    await asyncio.sleep(0.01)  # let the waiter enter the gather
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, timeout=2.0)
    child.cancel()
    await asyncio.gather(child, return_exceptions=True)


def test_observer_cleanup_loops_use_the_helper():
    """Structural lock: the bare swallow must not come back."""
    from ppsspp_dfx_mcp.core import game_state_observer as gso

    for fn in (gso.GameStateObserver.stop, gso.GameStateObserver.stop_gpu_stats_feed):
        src = inspect.getsource(fn)
        assert "await_cancelled(" in src, fn.__qualname__
        assert "except asyncio.CancelledError:\n                    pass" not in src
