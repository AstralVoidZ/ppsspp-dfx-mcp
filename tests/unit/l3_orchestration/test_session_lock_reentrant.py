"""D3 regression: per-session lock must be same-task reentrant.

A batch_step holds the session lock for its whole body; its embedded
screenshot step opens session_client again in the same task. With a
plain asyncio.Lock that nested acquire deadlocked and failed with
SESSION_BUSY after 5s. These tests lock in the reentrancy semantics of
_ReentrantSessionLock (same-task reentry allowed and depth-counted,
cross-task exclusion unchanged, non-owner release rejected, cancel-safe
wait).
"""

from __future__ import annotations

import asyncio

import pytest

from ppsspp_dfx_mcp.session.session_manager import _ReentrantSessionLock


@pytest.mark.asyncio
async def test_same_task_reacquire_succeeds_without_blocking() -> None:
    lock = _ReentrantSessionLock()
    await lock.acquire()
    # Nested acquire (the batch→screenshot pattern) must not deadlock.
    await asyncio.wait_for(lock.acquire(), timeout=1.0)
    await asyncio.wait_for(lock.acquire(), timeout=1.0)
    assert lock.locked()
    lock.release()
    lock.release()
    # Still held at depth 1 after two nested releases.
    assert lock.locked()
    lock.release()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_release_below_depth_or_non_owner_raises() -> None:
    lock = _ReentrantSessionLock()
    await lock.acquire()
    lock.release()
    with pytest.raises(RuntimeError):
        lock.release()  # over-release

    other = _ReentrantSessionLock()
    await other.acquire()

    # NB: on Python 3.12+ asyncio.wait_for(coro) runs the coroutine in the
    # SAME task — a genuinely different task requires create_task.
    async def stranger() -> None:
        other.release()  # must raise: wrong task

    stranger_task = asyncio.create_task(stranger())
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(stranger_task, timeout=1.0)
    # The failed stranger must not have disturbed ownership.
    assert other.locked()
    other.release()


@pytest.mark.asyncio
async def test_cross_task_exclusion_unchanged() -> None:
    lock = _ReentrantSessionLock()
    await lock.acquire()
    entered = asyncio.Event()
    release_outer = asyncio.Event()

    async def contender() -> None:
        entered.set()
        await lock.acquire()  # must wait until the outer task releases
        lock.release()

    task = asyncio.create_task(contender())
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    await asyncio.sleep(0.05)
    assert lock.locked()  # contender did not take ownership
    release_outer.set()
    lock.release()
    await asyncio.wait_for(task, timeout=2.0)
    assert not lock.locked()


@pytest.mark.asyncio
async def test_cross_task_acquire_times_out_with_session_busy_semantics() -> None:
    lock = _ReentrantSessionLock()
    await lock.acquire()

    # create_task (not wait_for-on-coroutine): 3.12+ runs the latter in the
    # same task, which would re-enter instead of contending.
    contender = asyncio.create_task(lock.acquire())
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(contender, timeout=0.1)
    contender.cancel()

    # Timed-out waiter must not have taken or corrupted ownership.
    lock.release()
    assert not lock.locked()
    await lock.acquire()  # reusable after release
    lock.release()


@pytest.mark.asyncio
async def test_nested_contextmanager_pattern_no_deadlock() -> None:
    """Mirror the batch→screenshot nesting shape at the helper level."""
    lock = _ReentrantSessionLock()
    inner_entered = asyncio.Event()

    async def outer() -> None:
        await asyncio.wait_for(lock.acquire(), timeout=1.0)
        try:
            # The "embedded screenshot" nested call.
            await asyncio.wait_for(lock.acquire(), timeout=1.0)
            inner_entered.set()
            lock.release()
        finally:
            lock.release()

    await asyncio.wait_for(outer(), timeout=2.0)
    assert inner_entered.is_set()
    assert not lock.locked()
