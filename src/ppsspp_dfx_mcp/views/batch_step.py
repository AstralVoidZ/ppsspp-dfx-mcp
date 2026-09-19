"""Batch step response views (FrozenModel)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.models.batch_step import BatchResult, StepResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class StepResultView(FrozenModel):
    """Public view of a single step result."""

    index: int = Field(description="Step position (0-based)")
    type: str = Field(description="Step type executed")
    status: str = Field(description="Step status: 'success' / 'failure' / 'skipped'")
    error: str = Field(default="", description="Empty on success; error on failure")
    data: dict[str, Any] | None = Field(
        default=None, description="Step-type-specific result payload"
    )

    @classmethod
    def from_result(cls, r: StepResult) -> StepResultView:
        return cls(
            index=r.index,
            type=r.type,
            status=r.status,
            error=r.error,
            data=r.data,
        )


class BatchStepResponse(FrozenModel):
    """Public response of the ppsspp_batch_step tool."""

    action: str = Field(description="Always 'run'")
    total: int = Field(description="Total steps in the batch")
    executed: int = Field(description="Steps actually executed (excludes skipped)")
    succeeded: int = Field(description="Steps with status='success'")
    failed: int = Field(description="Steps with status='failure'")
    skipped: int = Field(description="Steps with status='skipped'")
    recording_mode: bool = Field(
        description="Whether session was recording a replay when batch ran"
    )
    results: list[StepResultView] = Field(
        default_factory=list, description="Per-step results, in order"
    )
    aborted: bool = Field(default=False, description="Whether batch aborted early on failure")
    abort_reason: str = Field(default="", description="Empty if not aborted")

    @classmethod
    def from_result(cls, r: BatchResult) -> BatchStepResponse:
        return cls(
            action=r.action,
            total=r.total,
            executed=r.executed,
            succeeded=r.succeeded,
            failed=r.failed,
            skipped=r.skipped,
            recording_mode=r.recording_mode,
            results=[StepResultView.from_result(s) for s in r.results],
            aborted=r.aborted,
            abort_reason=r.abort_reason,
        )


class BatchSubmitResponse(FrozenModel):
    """Public response of ppsspp_batch_step with background=true (A1).

    Returned immediately after submission — the batch keeps executing on a
    detached server task that survives the MCP client's tool-call timeout.
    Poll ``ppsspp_batch_status(batch_id=...)`` for progress and the final
    result; cancel via ``ppsspp_batch_cancel``.
    """

    action: str = Field(description="Always 'submitted'")
    batch_id: str = Field(description="Job id for ppsspp_batch_status / _cancel")
    session_id: str = Field(description="Session the batch will execute on")
    total: int = Field(description="Total steps in the batch")
    estimated_s: float = Field(description="Heuristic wall-clock estimate in seconds")
    hint: str = Field(description="How to poll / cancel this background batch")


class BatchStatusResponse(FrozenModel):
    """Public response of the ppsspp_batch_status tool (A1).

    Lock-free by contract: polling never opens the session's WS transport
    and never waits for the per-session lock, so it is safe to call while a
    background batch (or any other tool) owns the session.
    """

    batch_id: str = Field(description="Job id")
    session_id: str = Field(description="Session the batch runs on")
    status: str = Field(
        description=(
            "'queued' / 'running' / 'completed' / 'failed' / 'cancelled' "
            "(protocol Tasks mapping: 'queued'→'working')"
        )
    )
    executed: int = Field(description="Steps executed so far")
    total: int = Field(description="Total steps in the batch")
    error: str | None = Field(default=None, description="Error message if failed/cancelled (null when none; real runner emits null)")
    result: dict[str, Any] | None = Field(
        default=None,
        description=("Final ppsspp_batch_step-shaped response; present once the batch completed"),
    )
    retention_jobs: int = Field(
        description=(
            "Finished-job retention window of the registry (in job count, "
            "not seconds): completed/failed/cancelled jobs beyond the "
            "oldest this many are evicted. Precursor of the protocol-level "
            "Task ttl."
        )
    )


class BatchJobSummaryView(FrozenModel):
    """One retained background batch job in the ppsspp_batch_list survey."""

    batch_id: str = Field(description="Job id (for ppsspp_batch_status / _cancel)")
    session_id: str = Field(description="Session the batch runs / ran on")
    status: str = Field(
        description=(
            "'queued' / 'running' / 'completed' / 'failed' / 'cancelled' "
            "(protocol Tasks mapping: 'queued'→'working')"
        )
    )
    executed: int = Field(description="Steps executed so far")
    total: int = Field(description="Total steps in the batch")
    error: str = Field(default="", description="Error message if failed/cancelled")
    result_present: bool = Field(
        description=(
            "Whether the final result is retained (true once completed; "
            "fetch it via ppsspp_batch_status)"
        )
    )


class BatchListResponse(FrozenModel):
    """Public response of the ppsspp_batch_list tool.

    tasks/list-shaped survey over the background job registry — closes the
    one gap between the batch trio and protocol-level MCP Tasks

    """

    jobs: list[BatchJobSummaryView] = Field(
        default_factory=list,
        description="All retained jobs, in submission order",
    )
    retention_jobs: int = Field(
        description=(
            "Finished-job retention window (in job count): finished jobs "
            "beyond the oldest this many are evicted and absent from jobs"
        )
    )
