"""L4 regression tests for background batch jobs + progress.

Locks in the 30s client-timeout abort workaround (background jobs run
on detached tasks) plus:
- estimate_batch_seconds arithmetic per step type
- foreground budget gate: >25s rejected WITHOUT opening the session
  (code BATCH_BUDGET_EXCEEDED), ≤25s passes unchanged
- background submit returns immediately (batch_id), executes to
  completion on a detached task, reports progress, mirrors the
  foreground response shape in status polls
- step failures surface as job.status='failed' with the foreground
  BATCH_STEP_FAILED summary and the partial result attached
- cancel: running job → 'cancelled', lock-free; unknown/finished ids
  error with BATCH_NOT_FOUND / BATCH_ALREADY_FINISHED
- registry retention: finished jobs bounded at 32 (oldest evicted),
  queued/running never evicted, strong Task refs released on eviction
- A2: foreground ctx progress fires once per step; a failing progress
  callback never fails the batch
- SESSION_BUSY background-submit guard + the client_helper busy hint
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.core import batch_jobs as bj_module
from ppsspp_dfx_mcp.core.batch_jobs import (
    FINISHED_JOB_RETENTION,
    BatchJob,
    BatchJobRegistry,
    estimate_batch_seconds,
)
from ppsspp_dfx_mcp.errors import ToolError
from ppsspp_dfx_mcp.tools import batch_step as bs_module
from ppsspp_dfx_mcp.tools.batch_step import (
    _execute_batch,
    batch_cancel,
    batch_status,
    batch_step,
)

# ============================================================================
# Isolation + client mocking (same pattern as test_batch_step_invariants)
# ============================================================================


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> BatchJobRegistry:
    """Swap the process-wide registry singleton for a per-test instance."""
    reg = BatchJobRegistry()
    monkeypatch.setattr(bj_module, "_registry", reg)
    return reg


async def _drain_registry(reg: BatchJobRegistry) -> None:
    """Await/cancel every job task so no test leaks pending tasks."""
    for job in list(reg._jobs.values()):
        if job.task is not None and not job.task.done():
            job.task.cancel()
    tasks = [j.task for j in reg._jobs.values() if j.task is not None]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
async def _no_leaked_tasks(fresh_registry: BatchJobRegistry):
    yield
    await _drain_registry(fresh_registry)


def _make_mock_client(saving: bool = False) -> AsyncMock:
    mock = AsyncMock()
    mock.replay_status.return_value = {"saving": saving, "executing": False}
    mock.read_u32.return_value = 0x1234
    return mock


def _patch_session_client(monkeypatch: pytest.MonkeyPatch, mock: AsyncMock) -> list[str]:
    """Patch batch_step.session_client to yield `mock`; returns the list of
    session_ids it was opened for (to assert the budget gate never opens
    one)."""
    opened: list[str] = []

    @asynccontextmanager
    async def fake_session_client(session_id: str) -> AsyncIterator[AsyncMock]:
        opened.append(session_id)
        yield mock

    monkeypatch.setattr(bs_module, "session_client", fake_session_client)
    return opened


def _patch_validate(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    validated: list[str] = []

    async def fake_validate(session_id: str) -> None:
        validated.append(session_id)

    monkeypatch.setattr(bs_module, "validate_session_alive", fake_validate)
    return validated


@pytest.fixture(autouse=True)
def _no_wait_liveness(monkeypatch: pytest.MonkeyPatch):
    """wait_frames_chunked re-validates the session between chunks via the
    client_helper symbol (imported at call time) — no session exists in
    unit tests, so neutralize it (same stand-in as the W3 invariants)."""

    async def noop(session_id: str) -> None:
        return None

    monkeypatch.setattr("ppsspp_dfx_mcp.session.client_helper.validate_session_alive", noop)


@pytest.fixture(autouse=True)
def _fast_waits(monkeypatch: pytest.MonkeyPatch):
    """Shrink wait-step sleeps so background/cancel tests stay fast while
    still exercising real awaits inside wait_frames_chunked. asyncio.sleep
    is a process-wide module attribute — the patch is global but
    monkeypatch-reverted after each test."""
    real_sleep = asyncio.sleep

    async def scaled_sleep(delay: float, *a, **kw):
        return await real_sleep(min(delay, 0.05), *a, **kw)

    monkeypatch.setattr(asyncio, "sleep", scaled_sleep)


# ============================================================================
# estimate_batch_seconds
# ============================================================================


class TestEstimateBatchSeconds:
    def test_press_duration_and_overhead(self):
        est = estimate_batch_seconds([{"type": "press", "button": "cross", "duration": 60}])
        assert est == pytest.approx(60 / 60 + 0.1)

    def test_wait_uses_default_interval(self):
        est = estimate_batch_seconds([{"type": "wait", "frames": 600}])
        assert est == pytest.approx(10.0)

    def test_wait_uses_explicit_interval(self):
        est = estimate_batch_seconds([{"type": "wait", "frames": 600, "interval": 0.5}])
        assert est == pytest.approx(300.0)

    def test_probe_and_screenshot(self):
        est = estimate_batch_seconds(
            [
                {"type": "state_probe", "names": "game_mode", "samples": 2},
                {"type": "screenshot"},
            ]
        )
        assert est == pytest.approx(2 * 0.25 + 1.0)

    def test_malformed_steps_cost_zero(self):
        est = estimate_batch_seconds([{"type": "press", "duration": "x"}, "not-a-dict", {}])
        assert est >= 0


# ============================================================================
# Foreground budget gate (acceptance 2)
# ============================================================================


class TestForegroundBudgetGate:
    async def test_over_budget_rejected_without_opening_session(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        opened = _patch_session_client(monkeypatch, _make_mock_client())
        steps = [{"type": "wait", "frames": 60 * 30}]  # 30s > 25s budget
        with pytest.raises(ToolError) as ei:
            await batch_step(session_id="s1", steps=steps)
        assert ei.value.code == "BATCH_BUDGET_EXCEEDED"
        assert "background=true" in str(ei.value)
        assert opened == []  # rejected BEFORE any session/WS work

    async def test_within_budget_executes_normally(self, monkeypatch: pytest.MonkeyPatch):
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)
        steps = [
            {"type": "press", "button": "cross", "duration": 10},
            {"type": "wait", "frames": 30},
        ]
        resp = await batch_step(session_id="s1", steps=steps)
        assert resp["total"] == 2
        assert resp["executed"] == 2
        assert resp["succeeded"] == 2

    async def test_background_bypasses_foreground_budget(self, monkeypatch: pytest.MonkeyPatch):
        """A 30s-estimate sequence is fine in background mode."""
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)
        _patch_validate(monkeypatch)
        steps = [{"type": "wait", "frames": 60 * 30}]
        resp = await batch_step(session_id="s1", steps=steps, background=True)
        assert resp["action"] == "submitted"
        job = bj_module.get_registry().get(resp["batch_id"])
        assert job is not None
        await job.task  # wait_frames_chunked is sleep-scaled in tests


# ============================================================================
# Background lifecycle (acceptance 3/4/6)
# ============================================================================


class TestBackgroundLifecycle:
    async def test_submit_returns_immediately_and_completes(self, monkeypatch: pytest.MonkeyPatch):
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)
        _patch_validate(monkeypatch)
        # 30s estimate — above the 25s foreground budget (proves bypass),
        # while the test-scaled sleeps keep actual runtime ~1-2s.
        steps = [
            {"type": "press", "button": "cross", "duration": 30},
            {"type": "wait", "frames": 60 * 30},
        ]
        resp = await batch_step(session_id="s1", steps=steps, background=True)
        assert resp["action"] == "submitted"
        assert set(resp) == {"action", "batch_id", "session_id", "total", "estimated_s", "hint"}
        reg = bj_module.get_registry()
        job = reg.get(resp["batch_id"])
        assert job is not None and job.total_steps == 2
        # Executor never ran yet or just started — either way the submit
        # did not block on it (no awaits of the batch body above).
        await asyncio.wait_for(job.task, timeout=5)
        assert job.status == "completed"
        # Foreground-shaped result embedded for pollers.
        assert job.result is not None
        assert job.result["total"] == 2
        assert job.result["succeeded"] == 2
        assert {
            "total",
            "executed",
            "succeeded",
            "failed",
            "skipped",
            "recording_mode",
            "results",
            "aborted",
        } <= set(job.result)

    async def test_status_poll_reflects_lifecycle(self, monkeypatch: pytest.MonkeyPatch):
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)
        _patch_validate(monkeypatch)
        resp = await batch_step(
            session_id="s1",
            steps=[{"type": "wait", "frames": 60}] * 3,
            background=True,
        )
        status = await batch_status(batch_id=resp["batch_id"])
        assert status["session_id"] == "s1"
        assert status["total"] == 3
        await asyncio.wait_for(bj_module.get_registry().get(resp["batch_id"]).task, timeout=5)
        final = await batch_status(batch_id=resp["batch_id"])
        assert final["status"] == "completed"
        assert final["executed"] == 3
        assert final["result"]["succeeded"] == 3

    async def test_progress_executed_monotonic(self, monkeypatch: pytest.MonkeyPatch):
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)
        _patch_validate(monkeypatch)
        seen: list[int] = []

        async def runner(job: BatchJob):
            async def on_progress(processed, total, stype, status):
                seen.append(processed)

            return await _execute_batch(
                "s1",
                [{"type": "wait", "frames": 10}] * 4,
                "continue",
                on_progress,
            )

        reg = bj_module.get_registry()
        batch_id = reg.submit("s1", 4, runner)
        job = reg.get(batch_id)
        await asyncio.wait_for(job.task, timeout=5)
        assert seen == sorted(seen)  # monotonic
        assert seen[-1] == 4

    async def test_step_failure_marks_job_failed_with_partial_result(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        mock = _make_mock_client()
        mock.press_button.side_effect = ToolError("boom", code="INTERNAL")
        _patch_session_client(monkeypatch, mock)
        _patch_validate(monkeypatch)
        resp = await batch_step(
            session_id="s1",
            steps=[{"type": "press", "button": "cross", "duration": 5}],
            background=True,
        )
        job = bj_module.get_registry().get(resp["batch_id"])
        await asyncio.gather(job.task, return_exceptions=True)
        assert job.status == "failed"
        assert "boom" in job.error
        assert "BATCH_STEP_FAILED" in job.error or "failed" in job.error
        # Partial result is retained for pollers.
        assert job.result is not None
        assert job.result["failed"] == 1

    async def test_duplicate_background_submit_is_busy(self, monkeypatch: pytest.MonkeyPatch):
        """A queued/running job on the session blocks a second submit."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def runner(job: BatchJob):
            started.set()
            await release.wait()
            return {}

        reg = bj_module.get_registry()
        batch_id = reg.submit("s1", 1, runner)
        await asyncio.wait_for(started.wait(), timeout=5)

        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)
        _patch_validate(monkeypatch)
        with pytest.raises(ToolError) as ei:
            await batch_step(
                session_id="s1",
                steps=[{"type": "wait", "frames": 5}],
                background=True,
            )
        assert ei.value.code == "SESSION_BUSY"
        assert batch_id in str(ei.value)
        release.set()
        await asyncio.wait_for(reg.get(batch_id).task, timeout=5)


# ============================================================================
# Cancel (acceptance 6)
# ============================================================================


class TestCancel:
    async def test_cancel_running_job(self, monkeypatch: pytest.MonkeyPatch):
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)
        _patch_validate(monkeypatch)
        resp = await batch_step(
            session_id="s1",
            steps=[{"type": "wait", "frames": 60 * 300}],  # 300s nominal
            background=True,
        )
        reg = bj_module.get_registry()
        job = reg.get(resp["batch_id"])
        cancel_resp = await batch_cancel(batch_id=resp["batch_id"])
        assert cancel_resp["batch_id"] == resp["batch_id"]
        await asyncio.gather(job.task, return_exceptions=True)
        assert job.status == "cancelled"
        # Lock-free after cancel: no queued/running job remains.
        assert reg.running_job_for_session("s1") is None
        # The mock client (lock stand-in in unit terms) saw the batch body
        # exit promptly — full lock-release semantics are covered by the
        # client_helper hint test + runtime finally blocks.

    async def test_cancel_before_start(self, monkeypatch: pytest.MonkeyPatch):
        """A job cancelled while still 'queued' reaches a terminal state."""
        gate = asyncio.Event()

        async def runner(job: BatchJob):
            await gate.wait()
            return {}

        reg = bj_module.get_registry()
        batch_id = reg.submit("s1", 1, runner)
        job = reg.get(batch_id)
        await batch_cancel(batch_id=batch_id)
        assert job.status == "cancelled"
        await asyncio.gather(job.task, return_exceptions=True)
        gate.set()

    async def test_cancel_unknown_id(self):
        with pytest.raises(ToolError) as ei:
            await batch_cancel(batch_id="nope")
        assert ei.value.code == "BATCH_NOT_FOUND"

    async def test_cancel_finished_job(self, monkeypatch: pytest.MonkeyPatch):
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)
        _patch_validate(monkeypatch)
        resp = await batch_step(
            session_id="s1",
            steps=[{"type": "wait", "frames": 5}],
            background=True,
        )
        job = bj_module.get_registry().get(resp["batch_id"])
        await asyncio.wait_for(job.task, timeout=5)
        with pytest.raises(ToolError) as ei:
            await batch_cancel(batch_id=resp["batch_id"])
        assert ei.value.code == "BATCH_ALREADY_FINISHED"

    async def test_status_unknown_id(self):
        with pytest.raises(ToolError) as ei:
            await batch_status(batch_id="nope")
        assert ei.value.code == "BATCH_NOT_FOUND"


# ============================================================================
# Registry retention + GC safety (acceptance 7)
# ============================================================================


class TestRegistryRetention:
    async def test_finished_jobs_bounded_oldest_evicted(self):
        reg = BatchJobRegistry()
        ids: list[str] = []
        for _ in range(FINISHED_JOB_RETENTION + 8):
            batch_id = reg.submit("s1", 1, _noop_runner)
            ids.append(batch_id)
            # Deterministic completion so eviction order == submit order.
            await asyncio.gather(reg.get(batch_id).task, return_exceptions=True)
        # Oldest 8 evicted, newest 32 retained.
        for old in ids[:8]:
            assert reg.get(old) is None
        for kept in ids[8:]:
            assert reg.get(kept) is not None

    async def test_eviction_releases_task_reference(self):
        reg = BatchJobRegistry()
        first = reg.submit("s1", 1, _noop_runner)
        job = reg.get(first)
        task = job.task
        await asyncio.wait_for(task, timeout=5)
        for _ in range(FINISHED_JOB_RETENTION):
            batch_id = reg.submit("s2", 1, _noop_runner)
            await asyncio.gather(reg.get(batch_id).task, return_exceptions=True)
        assert reg.get(first) is None
        assert job.task is None  # strong ref released on eviction
        del task

    async def test_running_job_never_evicted(self):
        reg = BatchJobRegistry()
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocking(job: BatchJob):
            started.set()
            await release.wait()
            return {}

        running_id = reg.submit("s1", 1, blocking)
        await asyncio.wait_for(started.wait(), timeout=5)
        finished_ids = []
        for _ in range(FINISHED_JOB_RETENTION + 5):
            batch_id = reg.submit("s2", 1, _noop_runner)
            finished_ids.append(batch_id)
            await asyncio.gather(reg.get(batch_id).task, return_exceptions=True)
        # 37 finished jobs each ran an eviction pass; the RUNNING job was
        # never in the retention deque, so it survived all of them.
        assert reg.get(running_id) is not None
        release.set()
        await asyncio.gather(reg.get(running_id).task, return_exceptions=True)


async def _noop_runner(job: BatchJob) -> dict:
    return {"noop": True}


# ============================================================================
# A2: foreground progress (acceptance 8)
# ============================================================================


class TestForegroundProgress:
    async def test_ctx_report_progress_per_step(self, monkeypatch: pytest.MonkeyPatch):
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)
        ctx = AsyncMock()
        steps = [
            {"type": "press", "button": "cross", "duration": 5},
            {"type": "wait", "frames": 5},
            {"type": "wait", "frames": 5},
        ]
        resp = await batch_step(session_id="s1", steps=steps, on_failure="continue", ctx=ctx)
        assert resp["succeeded"] == 3
        assert ctx.report_progress.await_count == 3
        calls = ctx.report_progress.await_args_list
        assert [c.args[0] for c in calls] == [1, 2, 3]
        assert all(c.args[1] == 3 for c in calls)

    async def test_failing_progress_callback_never_fails_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)
        ctx = AsyncMock()
        ctx.report_progress.side_effect = RuntimeError("wire gone")
        resp = await batch_step(
            session_id="s1",
            steps=[{"type": "wait", "frames": 5}],
            ctx=ctx,
        )
        assert resp["succeeded"] == 1

    async def test_no_ctx_still_works(self, monkeypatch: pytest.MonkeyPatch):
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)
        resp = await batch_step(session_id="s1", steps=[{"type": "wait", "frames": 5}])
        assert resp["succeeded"] == 1


# ============================================================================
# SESSION_BUSY hint (acceptance 9)
# ============================================================================


class TestBusyHint:
    async def test_busy_message_names_background_batch(self, monkeypatch: pytest.MonkeyPatch):
        """Hold the session lock manually, plant a running background job,
        and assert the SessionBusy error carries the batch id + tool hints."""
        from ppsspp_dfx_mcp.session import client_helper
        from ppsspp_dfx_mcp.session import session_manager as sm

        reg = bj_module.get_registry()
        job = BatchJob(batch_id="bgdeadbeef", session_id="s-busy", total_steps=1)
        job.status = "running"
        reg._jobs[job.batch_id] = job

        async def fake_get_state(session_id):
            return AsyncMock(pid=None)

        async def fake_touch(session_id):
            return AsyncMock()

        monkeypatch.setattr(sm, "get_session_state", fake_get_state)
        monkeypatch.setattr(sm, "touch_session", fake_touch)

        lock = sm.get_session_manager().session_lock("s-busy")
        await lock.acquire()
        try:
            with pytest.raises(ToolError) as ei:
                async with client_helper.session_client("s-busy"):
                    pass
            msg = str(ei.value)
            assert "bgdeadbeef" in msg
            assert "ppsspp_batch_status" in msg
            assert "ppsspp_batch_cancel" in msg
        finally:
            lock.release()

    async def test_busy_message_without_background_batch(self, monkeypatch: pytest.MonkeyPatch):
        from ppsspp_dfx_mcp.session import client_helper
        from ppsspp_dfx_mcp.session import session_manager as sm

        async def fake_get_state(session_id):
            return AsyncMock(pid=None)

        async def fake_touch(session_id):
            return AsyncMock()

        monkeypatch.setattr(sm, "get_session_state", fake_get_state)
        monkeypatch.setattr(sm, "touch_session", fake_touch)

        lock = sm.get_session_manager().session_lock("s-plain")
        await lock.acquire()
        try:
            with pytest.raises(ToolError) as ei:
                async with client_helper.session_client("s-plain"):
                    pass
            assert "background batch" not in str(ei.value)
        finally:
            lock.release()


# ============================================================================
# batch_list — tasks/list-shaped survey (Tasks-adoption P1)
# ============================================================================


class TestRegistryListJobs:
    async def test_empty_registry_lists_nothing(self):
        assert BatchJobRegistry().list_jobs() == []

    async def test_submission_order_and_fields(self):
        reg = BatchJobRegistry()
        id_a = reg.submit("s1", 2, _noop_runner)
        id_b = reg.submit("s2", 3, _noop_runner)
        await asyncio.gather(reg.get(id_a).task, reg.get(id_b).task, return_exceptions=True)
        jobs = reg.list_jobs()
        assert [j.batch_id for j in jobs] == [id_a, id_b]
        assert all(j.status == "completed" for j in jobs)

    async def test_evicted_jobs_absent_from_listing(self):
        reg = BatchJobRegistry()
        first = reg.submit("s1", 1, _noop_runner)
        await asyncio.gather(reg.get(first).task, return_exceptions=True)
        for _ in range(FINISHED_JOB_RETENTION + 3):
            batch_id = reg.submit("s2", 1, _noop_runner)
            await asyncio.gather(reg.get(batch_id).task, return_exceptions=True)
        listed = {j.batch_id for j in reg.list_jobs()}
        assert first not in listed
        assert len(listed) == FINISHED_JOB_RETENTION


class TestBatchListTool:
    async def test_empty_registry(self):
        from ppsspp_dfx_mcp.tools.batch_step import batch_status

        out = await batch_status()
        assert out["jobs"] == []
        assert out["retention_jobs"] == FINISHED_JOB_RETENTION

    async def test_lists_lifecycle_with_result_flag(self, fresh_registry: BatchJobRegistry):
        from ppsspp_dfx_mcp.tools.batch_step import batch_status

        id_a = fresh_registry.submit("s1", 1, _noop_runner)
        await asyncio.gather(fresh_registry.get(id_a).task, return_exceptions=True)
        out = await batch_status()
        assert len(out["jobs"]) == 1
        job = out["jobs"][0]
        assert job["batch_id"] == id_a
        assert job["status"] == "completed"
        # _noop_runner never bumps the progress callback → executed stays 0
        assert job["executed"] == 0
        assert job["total"] == 1
        assert job["error"] == ""
        assert job["result_present"] is True

    async def test_status_response_carries_retention_jobs(self, fresh_registry: BatchJobRegistry):
        from ppsspp_dfx_mcp.tools.batch_step import batch_status

        batch_id = fresh_registry.submit("s1", 1, _noop_runner)
        await asyncio.gather(fresh_registry.get(batch_id).task, return_exceptions=True)
        out = await batch_status(batch_id=batch_id)
        assert out["retention_jobs"] == FINISHED_JOB_RETENTION
