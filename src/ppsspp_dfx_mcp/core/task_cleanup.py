"""Await helpers for task cleanup paths (review-v4 W-7).

`except asyncio.CancelledError: pass` around ``await task`` cannot tell the
child's cancellation (expected — the cleanup just requested it) from an
outer cancellation of the awaiting coroutine itself (must keep
propagating: swallowing it makes the caller's task refuse to die and
corrupts cancel-scope accounting).
"""

from __future__ import annotations

import asyncio
from typing import Any


async def await_cancelled(task: asyncio.Task[Any]) -> None:
    """Await a task we just cancel()ed, swallowing only ITS cancellation.

    Implemented with ``gather(..., return_exceptions=True)`` because its
    cancellation semantics are exactly the two-way split this needs, and
    both halves were verified empirically on Python 3.13 (review-v4 W-7
    regression tests):

    - the child ending cancelled arrives as a RESULT (never raised), so
      the swallow cannot mask an outer cancellation;
    - an outer ``cancel()`` of the awaiting coroutine cancels the gather
      itself and raises here — propagated.

    (``task.cancelled()`` was tried first and is unsound: a cancelled
    child is indistinguishable from one the outer cancel took down with
    it, and ``current_task().cancelling()`` is reset once the error is
    delivered. Ordinary child exceptions re-raise for the caller's
    ``except Exception``.)
    """
    results = await asyncio.gather(task, return_exceptions=True)
    for outcome in results:
        if isinstance(outcome, asyncio.CancelledError):
            return
        if isinstance(outcome, BaseException):
            raise outcome
