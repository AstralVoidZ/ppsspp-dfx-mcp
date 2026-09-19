"""Background batch job registry (A1, plan_batch_background_and_progress_v1).

Why this exists: the ZCode MCP client enforces a ~30s tool-call timeout and
then sends ``notifications/cancelled``; the SDK translates that into an
anyio cancel-scope cancellation that aborts the tool coroutine. A batch
running inside the request's cancel scope therefore dies with it. A job
submitted through this registry runs on a detached ``asyncio.Task`` that
no request scope can cancel — the client times out, re-polls via
``ppsspp_batch_status``, and the batch keeps going.

Placement (core, not tools/): ``session/client_helper.py`` enriches the
SESSION_BUSY error with the running job's id, and the session layer may
import core but never tools.

Concurrency model: asyncio is single-threaded; every registry access
happens between awaits on the event loop, so plain dict/list mutations are
atomic. NOT thread-safe — do not touch from worker threads.

Memory: the registry holds a strong reference to each job's Task (Python's
asyncio GC would otherwise collect a running task with no other refs).
Finished jobs are retained (bounded) so a late status poll can still read
the result; eviction drops the Task reference too.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ppsspp_dfx_mcp.core.primitives import DEFAULT_FRAME_INTERVAL_S

logger = logging.getLogger(__name__)

__all__ = [
    "BACKGROUND_BUDGET_S",
    "FINISHED_JOB_RETENTION",
    "FOREGROUND_BUDGET_S",
    "BatchJob",
    "BatchJobRegistry",
    "estimate_batch_seconds",
    "get_registry",
]

# A foreground batch_step must finish comfortably inside the MCP client's
# 30s tool-call timeout, minus a 5s margin for transport/lock overhead.
FOREGROUND_BUDGET_S = 25.0

# A background batch may run arbitrarily long, but an unbounded steps list
# could pin the session lock for hours — cap the ESTIMATE, not the clock.
BACKGROUND_BUDGET_S = 3600.0

# How many finished jobs to keep for late status polls. Running/queued jobs
# are never evicted.
FINISHED_JOB_RETENTION = 32

# Heuristic per-step wall-clock costs (seconds) for the budget gate.
_PRESS_OVERHEAD_S = 0.1  # WS ticket RTT per press
_PROBE_PER_SAMPLE_S = 0.25
_SCREENSHOT_S = 1.0


def estimate_batch_seconds(
    steps: list[dict[str, Any]],
    probe_counts: dict[int, int] | None = None,
) -> float:
    """Estimate the wall-clock duration of a validated step list.

    Heuristic, used only for the budget gate — never for pacing:
    - press:   duration frames at 60fps + a fixed ticket-RTT overhead
    - wait:    frames × interval (interval defaults to 1/60, matching
               wait_frames_chunked)
    - state_probe: 0.25s per sample
    - screenshot: 1.0s flat
    - cpu_step: count × 0.05s (pause probe + step + broadcast confirm per
      instruction, ISS-001) + 0.5s for the with_stepping pause/resume dance

    Malformed steps cost 0 — validation happens before estimation.
    """
    total = 0.0
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        stype = step.get("type")
        if stype == "press":
            duration = step.get("duration", 1)
            total += (
                duration / 60.0 + _PRESS_OVERHEAD_S
                if isinstance(duration, (int, float)) and duration >= 0
                else _PRESS_OVERHEAD_S
            )
        elif stype == "cpu_step":
            # 🟡9: previously cost 0 — a [{cpu_step count=1000}] batch
            # passed the 25s foreground budget gate, then burned minutes
            # of wall clock (and got cancelled by the ~30s client).
            count = step.get("count", 1)
            total += count * 0.05 + 0.5 if isinstance(count, (int, float)) and count >= 1 else 0.5
        elif stype == "wait":
            frames = step.get("frames", 0)
            interval = step.get("interval")
            per_frame = (
                interval
                if isinstance(interval, (int, float)) and interval > 0
                else DEFAULT_FRAME_INTERVAL_S
            )
            total += frames * per_frame if isinstance(frames, (int, float)) and frames >= 0 else 0.0
        elif stype == "state_probe":
            samples = step.get("samples", 1)
            n_samples = samples if isinstance(samples, (int, float)) and samples >= 1 else 1
            # W12 (review v2): cost scales with PROBE COUNT, not just
            # samples — names='' observes every registered probe, so a
            # 50-probe × 1400-sample step used to estimate 350s while
            # actually burning ~70 minutes (all with the session lock
            # held). Callers resolve the per-step probe count; 1 is the
            # conservative floor for unknown shapes.
            n_probes = (probe_counts or {}).get(i, 1)
            total += max(0.25, n_samples * n_probes * _PROBE_PER_SAMPLE_S)
        elif stype == "screenshot":
            total += _SCREENSHOT_S
    return total


# Legal job statuses, in lifecycle order.
_JOB_STATUSES = ("queued", "running", "completed", "failed", "cancelled")


@dataclass
class BatchJob:
    """State of one background batch execution.

    ``task`` is the strong reference keeping the asyncio Task alive; it is
    released when the job is evicted from the registry's retention window.
    ``result`` (when completed) is the same dict shape the foreground
    ppsspp_batch_step returns, so pollers see one response shape.
    """

    batch_id: str
    session_id: str
    total_steps: int
    status: str = "queued"
    executed: int = 0
    result: dict[str, Any] | None = None
    error: str = ""
    task: asyncio.Task[Any] | None = field(default=None, repr=False)
    created_at: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    def summary(self) -> dict[str, Any]:
        """Poll-friendly view (excludes the Task reference)."""
        return {
            "batch_id": self.batch_id,
            "session_id": self.session_id,
            "status": self.status,
            "executed": self.executed,
            "total": self.total_steps,
            "error": self.error,
            "result": self.result,
        }


class BatchJobRegistry:
    """Owns detached background batch tasks (see module docstring)."""

    def __init__(self) -> None:
        self._jobs: dict[str, BatchJob] = {}
        self._finished_order: deque[str] = deque()

    def submit(
        self,
        session_id: str,
        total_steps: int,
        runner: Callable[[BatchJob], Awaitable[dict[str, Any]]],
    ) -> str:
        """Create a job and start it on a detached Task.

        ``runner`` receives the job (to bump ``executed`` via its progress
        callback) and returns the final result dict. The wrapper performs
        the queued→running→terminal transitions; a CancelledError is
        recorded as ``cancelled`` and then re-raised so asyncio task
        semantics stay intact.
        """
        batch_id = uuid.uuid4().hex[:12]
        job = BatchJob(
            batch_id=batch_id,
            session_id=session_id,
            total_steps=total_steps,
        )
        job.task = asyncio.create_task(self._run(job, runner))
        self._jobs[batch_id] = job
        return batch_id

    async def _run(
        self,
        job: BatchJob,
        runner: Callable[[BatchJob], Awaitable[dict[str, Any]]],
    ) -> None:
        job.status = "running"
        job.started_at = time.monotonic()
        try:
            job.result = await runner(job)
            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
            job.error = "cancelled via ppsspp_batch_cancel"
            raise
        except Exception as e:  # noqa: BLE001 — job must record, not crash
            job.status = "failed"
            job.error = str(e) or e.__class__.__name__
            logger.warning("background batch %s failed: %s", job.batch_id, e)
        finally:
            job.finished_at = time.monotonic()
            if job.status in ("completed", "failed", "cancelled"):
                self._finished_order.append(job.batch_id)
                self._evict_finished()

    def get(self, batch_id: str) -> BatchJob | None:
        """Look up a job by id (pure read; no session lock involved)."""
        return self._jobs.get(batch_id)

    def list_jobs(self) -> list[BatchJob]:
        """All retained jobs, in submission order (pure read).

        Finished jobs beyond the retention window are already evicted and
        therefore absent — this IS the tasks/list-shaped survey view.
        """
        return list(self._jobs.values())

    def running_job_for_session(self, session_id: str) -> BatchJob | None:
        """The queued/running job holding (or about to hold) this session."""
        for job in self._jobs.values():
            if job.session_id == session_id and job.status in ("queued", "running"):
                return job
        return None

    def cancel(self, batch_id: str) -> BatchJob:
        """Cancel a queued/running job; returns the updated job.

        Raises:
            KeyError: unknown batch_id.
            RuntimeError: job already in a terminal state.
        """
        job = self._jobs.get(batch_id)
        if job is None:
            raise KeyError(batch_id)
        if job.status in ("completed", "failed", "cancelled"):
            raise RuntimeError(f"batch {batch_id} already {job.status}; nothing to cancel")
        assert job.task is not None  # set synchronously by submit()
        job.task.cancel()
        if job.status == "queued":
            # Cancelled before the wrapper coroutine got its first slot:
            # CancelledError is thrown at coroutine entry, so _run's
            # except/finally never run — record the terminal state here
            # (no awaits between the check and task.cancel(): atomic on
            # the event loop; a started job is already 'running').
            job.status = "cancelled"
            job.error = "cancelled before start"
            job.finished_at = time.monotonic()
            self._finished_order.append(job.batch_id)
            self._evict_finished()
        return job

    def _evict_finished(self) -> None:
        """Drop the oldest finished jobs beyond the retention window."""
        while len(self._finished_order) > FINISHED_JOB_RETENTION:
            oldest = self._finished_order.popleft()
            job = self._jobs.pop(oldest, None)
            if job is not None:
                job.task = None  # release the strong Task reference


_registry: BatchJobRegistry | None = None


def get_registry() -> BatchJobRegistry:
    """Process-wide registry singleton (event-loop-thread only)."""
    global _registry
    if _registry is None:
        _registry = BatchJobRegistry()
    return _registry
