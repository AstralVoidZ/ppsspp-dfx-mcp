"""Batch step orchestration tool wrapper.

3 tools exposed:
- ppsspp_batch_step(session_id, steps, on_failure?, background?, ctx?)
    — execute a sequence of primitive steps in order. Foreground (default)
    runs inline; background=true submits to a detached server task that
    survives the MCP client's ~30s tool-call timeout and returns a
    batch_id immediately.
- ppsspp_batch_status(batch_id) — lock-free progress/result poll.
- ppsspp_batch_cancel(batch_id) — request cancellation of a queued/running job.
- ppsspp_batch_status(batch_id omitted) — survey all retained jobs.

Step types (5):
- 'press'        — call ppsspp_press_button (button + duration)
- 'wait'         — call ppsspp_wait_frames (frames)
- 'state_probe'  — call ppsspp_state_observer(action=observe) (names + samples)
- 'screenshot'   — call ppsspp_screenshot (source / mode)
- 'cpu_step'     — N CPU instructions (into/over/out)

Recording-mode aware:
- On entry, calls `client.replay_status()` to detect recording state.
- If `saving=true` (recording in progress), screenshot steps are
  marked 'skipped' and not executed: gpu.buffer.screenshot requires
  CPU stepping, which would break the recording.
- press / wait / state_probe steps execute normally during recording
  (read_u32 works fine in RUNNING state).

Progress: foreground calls report per-step MCP progress via the
injected Context (a no-op when the client sent no progressToken).
Background jobs have no request context — their progress lives in
ppsspp_batch_status.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypedDict, cast

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.core.batch_jobs import (
    BACKGROUND_BUDGET_S,
    FINISHED_JOB_RETENTION,
    FOREGROUND_BUDGET_S,
    BatchJob,
    estimate_batch_seconds,
    get_registry,
)
from ppsspp_dfx_mcp.core.primitives import MAX_PRESS_DURATION_FRAMES, MAX_WAIT_FRAMES
from ppsspp_dfx_mcp.errors import StepInvalid, ToolError
from ppsspp_dfx_mcp.models.batch_step import (
    STEP_TYPES,
    BatchResult,
    BatchStepInput,
    StepResult,
)
from ppsspp_dfx_mcp.models.input import PPSSPP_ALL_BUTTONS
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client, validate_session_alive
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract, flatten_union
from ppsspp_dfx_mcp.tools._common import (
    require_int_not_bool,
    save_output_bytes,
    translate_tool_errors,
    wait_frames_chunked,
)
from ppsspp_dfx_mcp.views.batch_step import (
    BatchJobSummaryView,
    BatchListResponse,
    BatchStatusResponse,
    BatchStepResponse,
    BatchSubmitResponse,
)
from ppsspp_dfx_mcp.views.state_observer import StateObserverResponse

# 联合契约必须扁平——func_metadata 不展开多继承 TypedDict，
# 只有第一个父类的键进入 outputSchema，其余分支的键被 structuredContent
# 静默剥离。flatten_union 生成单层全可选 TypedDict，跨 SDK 版本一致。
_BatchStepResponseOut = derive_output_contract(
    "BatchStepResponseOut", BatchStepResponse, partial=True
)
_BatchSubmitResponseOut = derive_output_contract(
    "BatchSubmitResponseOut", BatchSubmitResponse, partial=True
)
_BatchStatusResponseOut = derive_output_contract(
    "BatchStatusResponseOut", BatchStatusResponse, partial=True
)
_BatchListResponseOut = derive_output_contract(
    "BatchListResponseOut", BatchListResponse, partial=True
)

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    BatchStepOutput = dict[str, Any]
    BatchStatusOutput = dict[str, Any]
else:
    BatchStepOutput = flatten_union(
        "BatchStepOutput", _BatchStepResponseOut, _BatchSubmitResponseOut
    )
    BatchStatusOutput = flatten_union(
        "BatchStatusOutput", _BatchStatusResponseOut, _BatchListResponseOut
    )
BatchListOutput = derive_output_contract("BatchListOutput", BatchListResponse)


class BatchCancelOutput(TypedDict):
    """`ppsspp_batch_cancel` 的结构化返回契约。

    本工具直接构造 dict 字面量、**没有对应的 Pydantic view**，故手工声明而非派生
    （同 `list_addresses.ListAddressesOutput`）。字段与下文 `return {...}` 一致。
    """

    batch_id: str
    status: str
    note: str


logger = logging.getLogger(__name__)

__all__ = ["batch_step", "batch_status", "batch_cancel"]

# STEP_TYPES comes from models.batch_step (single source of truth, locked
# to the 4 step TypedDicts by an import-time assert there).
# Single source of truth — a stale copy here once diverged from the button
# table, so reference the canonical models.input tuple directly (e.g. 'home'
# is valid for ppsspp_press_button and must stay valid here).
_VALID_BUTTONS = PPSSPP_ALL_BUTTONS

# Progress callback: (processed, total, step_type, step_status) -> awaitable.
ProgressCallback = Callable[[int, int, str, str], Awaitable[None]]


def _validate_step(step: dict[str, Any], index: int) -> None:
    """Validate a single step dict structure. Raises StepInvalid on invalid."""
    if not isinstance(step, dict):
        raise StepInvalid(f"step[{index}] must be a dict; got {type(step).__name__}")
    if "type" not in step:
        raise StepInvalid(f"step[{index}] missing required 'type' field")
    stype = step["type"]
    if stype not in STEP_TYPES:
        # A-16 (review v4): a step whose shape is invalid is a
        # STEP_INVALID, not an ARGS_INVALID — the same classification the
        # missing-type / bad-button branches already use.
        raise StepInvalid(
            f"step[{index}] invalid type={stype!r}; expected one of {STEP_TYPES}",
        )
    if stype == "press":
        button = step.get("button")
        if not button:
            raise StepInvalid(f"step[{index}] type=press requires 'button' field")
        if button not in _VALID_BUTTONS:
            raise StepInvalid(
                f"step[{index}] invalid button={button!r}; expected one of {_VALID_BUTTONS}",
            )
        duration = step.get("duration", 1)
        # isinstance(True, int) is True — bools must be
        # rejected explicitly, same rule as wait_frames_chunked.
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            raise StepInvalid(
                f"step[{index}] duration must be int >= 0; got {duration!r}",
            )
        if duration > MAX_PRESS_DURATION_FRAMES:
            # WS ticket timeout scales with duration — an
            # unbounded press hung the whole batch indefinitely.
            raise StepInvalid(
                f"step[{index}] duration {duration} exceeds the cap {MAX_PRESS_DURATION_FRAMES}",
            )
    elif stype == "wait":
        frames = step.get("frames")
        if frames is None:
            raise StepInvalid(f"step[{index}] type=wait requires 'frames' field")
        if isinstance(frames, bool) or not isinstance(frames, int) or frames < 0:
            raise StepInvalid(
                f"step[{index}] frames must be int >= 0; got {frames!r}",
            )
        if frames > MAX_WAIT_FRAMES:
            # A-16: same step-shape classification as the duration cap.
            raise StepInvalid(
                f"step[{index}] frames {frames} exceeds the cap "
                f"{MAX_WAIT_FRAMES} (~{MAX_WAIT_FRAMES // 60}s of game time)",
            )
    elif stype == "state_probe":
        # names is optional (observe all); samples optional.
        samples = step.get("samples", 1)
        if isinstance(samples, bool) or not isinstance(samples, int) or samples < 1:
            raise StepInvalid(
                f"step[{index}] samples must be int >= 1; got {samples!r}",
            )
    # screenshot: no required fields (source / mode optional).
    elif stype == "cpu_step":
        cmode = step.get("mode")
        if cmode not in ("into", "over", "out"):
            raise StepInvalid(
                f"step[{index}] cpu_step requires mode='into'|'over'|'out'; got {cmode!r}"
            )
        ccount = step.get("count", 1)
        # `count=True` used to pass as a 1-instruction step.
        ccount = require_int_not_bool(ccount, f"step[{index}] cpu_step count", exc=StepInvalid)
        if ccount < 1 or ccount > 1000:
            raise StepInvalid(f"step[{index}] cpu_step count must be int 1..1000; got {ccount!r}")


_CPU_STEP_BUDGET_S = 10.0  # flat per-step ceiling; the batch deadline shrinks it


def _per_step_timeout(deadline: float | None, cap_s: float) -> float:
    """Per-step budget for one batch step (state_probe AND cpu_step).

    The foreground gate admits a batch by ESTIMATE, while a per-step
    ``wait_for``/``timeout_ms`` is the only place one step could run past
    what was promised: with a flat ceiling, a batch estimated at 24s could
    still contain steps that each burn the flat cap (the estimator floors
    a step at 0.05-0.25s). Deriving the ceiling from the batch deadline
    keeps the number in the same units as the gate; the flat cap remains
    the absolute ceiling for background batches (whose deadline is an hour
    out) and for callers that pass no budget at all.
    """
    if deadline is None:
        return cap_s
    return max(1.0, min(cap_s, deadline - time.monotonic()))


async def _execute_batch(
    session_id: str,
    steps: list[dict[str, Any]],
    on_failure: str,
    on_progress: ProgressCallback | None = None,
    budget_s: float | None = None,
) -> BatchResult:
    """Run the step loop under the session lock. Returns the BatchResult.

    Extracted verbatim from the former batch_step body so the foreground
    and background paths share one implementation. Raises ToolError
    for session-level failures (SESSION_NOT_FOUND / SESSION_BUSY / ...);
    per-step failures are recorded in the result, not raised.

    ``on_progress``, when given, is awaited after every step with
    (processed, total, step_type, step_status); exceptions it raises are
    logged and swallowed — progress reporting must never fail a batch.

    ``budget_s`` is the wall-clock budget the CALLER
    promised the client for this whole batch (foreground:
    ``FOREGROUND_BUDGET_S``; background: ``BACKGROUND_BUDGET_S``). The
    deadline is computed here, before the session lock is acquired, so
    lock wait is charged to the same budget the gate used.
    """
    results: list[StepResult] = []
    executed = succeeded = failed = skipped = 0
    recording_mode = False
    aborted = False
    abort_reason = ""
    deadline = time.monotonic() + budget_s if budget_s is not None else None

    async with session_client(session_id) as client:
        # Detect recording mode (screenshot is forbidden during recording).
        try:
            status_resp = await client.replay_status()
            recording_mode = bool(status_resp.get("saving", False))
        except Exception as e:
            # If replay_status fails (e.g. old PPSSPP without replay),
            # assume not recording and continue.
            logger.warning("replay_status probe failed in batch_step: %s", e)
            recording_mode = False

        for i, step in enumerate(steps):
            stype = step["type"]
            # Screenshot in recording mode → skip.
            if stype == "screenshot" and recording_mode:
                results.append(
                    StepResult(
                        index=i,
                        type=stype,
                        status="skipped",
                        error="screenshot forbidden during replay recording "
                        "(gpu.buffer.screenshot requires CPU stepping)",
                    )
                )
                skipped += 1
                await _report(on_progress, i + 1, len(steps), stype, "skipped")
                continue

            step_status = "success"
            step_error = ""
            step_data: dict[str, Any] | None = None

            try:
                if stype == "press":
                    button = step["button"]
                    duration = step.get("duration", 1)
                    await client.press_button(button=button, duration=duration)
                    step_data = {"button": button, "duration": duration}
                elif stype == "wait":
                    frames = step["frames"]
                    interval = step.get("interval")
                    # Shared chunked waiter — validates
                    # interval (<=0 used to silently become sleep(0),
                    # i.e. the wait step did nothing) and adds the same
                    # mid-sleep session-liveness checks as wait_frames.
                    elapsed = await wait_frames_chunked(frames, interval, session_id)
                    step_data = {"frames": frames, "elapsed_s": elapsed}
                elif stype == "state_probe":
                    # Reuse the batch_step's client to avoid opening a
                    # nested session_client (each session_client opens
                    # a fresh WS connection — see client_helper.py:14).
                    # Import locally to break circular dependency.
                    from ppsspp_dfx_mcp.service.probe_observer import (
                        PROBE_OBSERVE_BUDGET_S,
                        _observe_probes,
                        _resolve_target_probes,
                        _seed_from_yaml,
                    )

                    _seed_from_yaml(session_id)
                    names_str = step.get("names", "")
                    samples = step.get("samples", 1)
                    target_probes = _resolve_target_probes(names_str, session_id)
                    # Same whole-operation budget the
                    # state_observer tool applies — without it the batch
                    # path could wait per-probe forever (probes × samples
                    # × RTT) while holding the session lock.
                    # The ceiling is shared with the tool,
                    # and shrinks to whatever is left of the batch deadline
                    # so one step cannot outlive the budget the gate used.
                    observe_result = await asyncio.wait_for(
                        _observe_probes(client, target_probes, samples),
                        timeout=_per_step_timeout(deadline, PROBE_OBSERVE_BUDGET_S),
                    )
                    step_data = StateObserverResponse.from_observe(observe_result).model_dump(
                        mode="json"
                    )
                elif stype == "cpu_step":
                    # Real single-stepping (ISS-001). The debug_client
                    # primitives enforce REQUIRED_STEPPING themselves
                    # (with_stepping pause dance) and filter stale
                    # pause broadcasts, so a confirmed return means PC
                    # actually advanced. `out` may take longer on call
                    # boundaries — give each step a generous timeout.
                    cmode = step["mode"]
                    ccount = step.get("count", 1)
                    stepper = getattr(client, f"step_{cmode}")
                    stepped = 0
                    last_step_info: dict[str, Any] = {}
                    # with_stepping restores RUNNING for the rest of
                    # the batch — later press/wait steps must not act on a
                    # frozen CPU.
                    async with client.with_stepping():
                        try:
                            for _ in range(ccount):
                                # W-4: same deadline-derived budget as the
                                # probe branch — a flat 10s per step let one
                                # stalled step hold the session lock far past
                                # the budget the gate approved the batch by.
                                last_step_info = await stepper(
                                    timeout_ms=int(
                                        _per_step_timeout(deadline, _CPU_STEP_BUDGET_S) * 1000
                                    )
                                )
                                stepped += 1
                        except TimeoutError as e:
                            step_status = "failure"
                            step_error = (
                                f"cpu_step confirmed {stepped}/{ccount} steps then stalled: {e}"
                            )
                    pc_raw = last_step_info.get("pc", 0)
                    # pc may arrive as int (from WS) or hex str (from
                    # step helpers); format_address handles int, str
                    # passthrough preserves already-hex values.
                    last_pc = format_address(pc_raw) if isinstance(pc_raw, int) else str(pc_raw)
                    step_data = {
                        "mode": cmode,
                        "requested": ccount,
                        "stepped": stepped,
                        "last_pc": last_pc,
                        "last_ticks": last_step_info.get("ticks"),
                        "resumed_after": True,
                    }
                elif stype == "screenshot":
                    # Call the capture SERVICE, not the tool function: the
                    # tool wrapper would stack its error translator on top
                    # of this step's own handler (W19). The service uses
                    # session_capture (its own CaptureService + transport),
                    # so it opens a second WS connection — acceptable
                    # because the GPU buffer access requires a dedicated
                    # transport and PPSSPP supports concurrent WS links.
                    from ppsspp_dfx_mcp.service.screenshot_service import capture_frame

                    frame = await capture_frame(session_id, step.get("source"), step.get("mode"))
                    file_path = ""
                    if frame.data:
                        # The image is auto-saved to disk, matching what the
                        # tool does; only the metadata goes into step_data.
                        file_path = await save_output_bytes(
                            "screenshots", frame.output_filename(), frame.data
                        )
                    step_data = frame.meta(file_path)
            except ToolError as e:
                step_status = "failure"
                step_error = str(e)
            except Exception as e:
                step_status = "failure"
                step_error = str(e) or e.__class__.__name__

            executed += 1
            if step_status == "success":
                succeeded += 1
            else:
                failed += 1

            results.append(
                StepResult(
                    index=i,
                    type=stype,
                    status=step_status,
                    error=step_error,
                    data=step_data,
                )
            )
            await _report(on_progress, i + 1, len(steps), stype, step_status)

            # on_failure=abort: stop after first failure.
            if step_status == "failure" and on_failure == "abort":
                aborted = True
                abort_reason = f"step[{i}] type={stype} failed: {step_error}"
                break

    return BatchResult(
        action="run",
        total=len(steps),
        executed=executed,
        succeeded=succeeded,
        failed=failed,
        skipped=skipped,
        recording_mode=recording_mode,
        results=tuple(results),
        aborted=aborted,
        abort_reason=abort_reason,
    )


async def _report(
    on_progress: ProgressCallback | None,
    processed: int,
    total: int,
    stype: str,
    status: str,
) -> None:
    """Await the progress hook, never letting it fail the batch."""
    if on_progress is None:
        return
    try:
        await on_progress(processed, total, stype, status)
    except Exception as e:  # noqa: BLE001 — progress is best-effort
        logger.warning("batch progress callback failed (ignored): %s", e)


def _batch_failure_error(result: BatchResult) -> str:
    """The BATCH_STEP_FAILED summary text (stable format, kept verbatim).

    Empty when the batch is a clean success.
    """
    if not (result.failed or result.aborted):
        return ""
    failures = " | ".join(
        f"step[{r.index}] {r.type}: {r.error or 'failed'}"
        for r in result.results
        if r.status == "failure"
    )
    return (
        f"batch_step executed {result.executed} step(s): "
        f"{result.succeeded} succeeded, {result.failed} failed"
        + (f", {result.skipped} skipped" if result.skipped else "")
        + (f"; aborted: {result.abort_reason}" if result.aborted else "")
        + (f" | {failures}" if failures else "")
    )


def _make_ctx_progress(ctx: Context) -> ProgressCallback:
    """Per-step MCP progress notifier.

    No-op on the wire when the client sent no progressToken (SDK
    server/session.py report_progress contract); local exceptions are
    swallowed by _report so notification hiccups never kill a batch.
    """

    async def on_progress(processed: int, total: int, stype: str, status: str) -> None:
        await ctx.report_progress(processed, total, f"step {processed}/{total} {stype} {status}")

    return on_progress


def _make_job_progress(job: BatchJob) -> ProgressCallback:
    """Record executed-step count on the job for batch_status pollers."""

    async def on_progress(processed: int, total: int, stype: str, status: str) -> None:
        job.executed = processed

    return on_progress


# LONG-TOOL: ordered multi-step execution and detached background submission share one validation,
# budget-estimation and progress-reporting path, so the two branches stay in a single tool body.
@mcp.tool(
    name="ppsspp_batch_step",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
@translate_tool_errors
async def batch_step(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    steps: Annotated[
        list[BatchStepInput],
        Field(
            description=(
                "Ordered list of step dicts to execute. Each step must "
                "have a 'type' field. Supported types:\n"
                "- press: {type:'press', button:'cross', duration:30}\n"
                "- wait: {type:'wait', frames:60}\n"
                "- state_probe: {type:'state_probe', names:'game_mode', "
                "samples:1}\n"
                "- screenshot: {type:'screenshot', source:'render'}"
            ),
        ),
    ],
    on_failure: Annotated[
        Literal["continue", "abort"],
        Field(
            default="continue",
            description=(
                "What to do when a step fails (default 'continue'). "
                "'continue' keeps running subsequent steps; 'abort' stops "
                "the batch immediately. Screenshot steps that are skipped "
                "due to recording mode are NOT failures."
            ),
        ),
    ] = "continue",
    background: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Run on a detached server task that survives the MCP "
                "client's ~30s tool-call timeout (default false). "
                "Foreground calls exceeding the 25s budget are rejected "
                "with BATCH_BUDGET_EXCEEDED; background calls return a "
                "batch_id immediately — poll ppsspp_batch_status for "
                "progress and the final result, cancel via "
                "ppsspp_batch_cancel. Background jobs have no per-step "
                "MCP progress notifications; use the status poll."
            ),
        ),
    ] = False,
    ctx: Context | None = None,
) -> BatchStepOutput:
    """PURPOSE: Execute an ordered automation sequence of press / wait / state_probe / screenshot steps in one call, optionally on a detached background task that outlives the client timeout.

    USAGE: session_id + steps:[{type: press|wait|state_probe|screenshot, ...}]; on_failure='continue'|'abort' (default continue); background=false|true.


    ROUTING: ordered multi-step automation -> here; single CPU-step -> ppsspp_step; background job survey -> ppsspp_batch_status(batch_id omitted).
    BEHAVIOR: STATE-CHANGE. Foreground (default) holds the session lock for the whole batch; frames are 60fps wall-clock equivalents; sequences estimated >25s are rejected up-front with BATCH_BUDGET_EXCEEDED (the MCP client aborts tool calls at ~30s, killing the remaining steps server-side). Per-step MCP progress is reported when the client requests it. background=true validates and submits instantly, returns {action:'submitted', batch_id,...}, keeps the session lock for the batch duration, and reports progress via ppsspp_batch_status. If any foreground step fails the whole call is isError BATCH_STEP_FAILED — inspect results[] per step. Screenshots are auto-skipped during replay recording.

    RETURNS: foreground {total, executed, succeeded, failed, skipped, recording_mode, results[], aborted}; background {action:'submitted', batch_id, session_id, total, estimated_s, hint}."""
    if not isinstance(steps, list):
        raise StepInvalid(f"steps must be a list; got {type(steps).__name__}")
    if not steps:
        raise StepInvalid("steps must not be empty")
    # 契约面是 TypedDict 联合（wire inputSchema 依赖该注解）；内部校验/
    # 估算/执行路径按 dict[str, Any] 处理。TypedDict 运行时即 dict，故这里
    # 只是类型层面的窄化，不改变对象本身（list 不变性不允许直接传参）。
    step_dicts = cast(list[dict[str, Any]], steps)
    for i, step in enumerate(step_dicts):
        _validate_step(step, i)

    # Resolve the per-step ROUND-TRIP count so the estimate
    # reflects samples × merged block reads (adjacent probes fold into one
    # read; names='' expands to every probe).
    probe_round_trips: dict[int, int] = {}
    for _i, _st in enumerate(step_dicts):
        if _st.get("type") == "state_probe":
            try:
                from ppsspp_dfx_mcp.service.probe_observer import (
                    _resolve_target_probes as _rtp,
                )
                from ppsspp_dfx_mcp.service.probe_observer import (
                    _seed_from_yaml,
                )
                from ppsspp_dfx_mcp.service.scan_engine import _merge_runs

                # Mirror the executing state_probe path (line ~304): seed the
                # session's probe library from addresses.yaml FIRST, then
                # resolve WITH the session id. A one-argument call raised
                # TypeError, was swallowed by the best-effort `except` below,
                # and floored probe_round_trips at 1 for every probe batch —
                # silently collapsing the W31 gate (audit W31).
                _seed_from_yaml(session_id)
                _probes = _rtp(_st.get("names", ""), session_id)
                _addrs = sorted(p.address for p in _probes)
                _max_size = max((p.size for p in _probes), default=1)
                probe_round_trips[_i] = max(1, len(_merge_runs(_addrs, _max_size)))
            except Exception:
                probe_round_trips[_i] = 1
    estimated_s = estimate_batch_seconds(step_dicts, probe_round_trips)

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_batch_step",
            "session_id": session_id,
            "step_count": len(steps),
            "on_failure": on_failure,
            "background": background,
            "estimated_s": round(estimated_s, 2),
        },
    )

    if background:
        if estimated_s > BACKGROUND_BUDGET_S:
            raise ToolError(
                f"estimated batch duration {estimated_s:.0f}s exceeds the "
                f"background budget {BACKGROUND_BUDGET_S:.0f}s — split the "
                f"steps list",
                code="BATCH_BUDGET_EXCEEDED",
            )
        # Fail fast on a dead/unknown session before detaching the task
        # (also marks the session active for the idle GC).
        await validate_session_alive(session_id)
        registry = get_registry()
        existing = registry.running_job_for_session(session_id)
        if existing is not None:
            raise ToolError(
                f"session {session_id} already has a background batch "
                f"({existing.batch_id}) queued/running — poll "
                f"ppsspp_batch_status(batch_id='{existing.batch_id}') or "
                f"cancel it via ppsspp_batch_cancel before submitting "
                f"another",
                code="SESSION_BUSY",
            )

        async def runner(job: BatchJob) -> dict[str, Any]:
            result = await _execute_batch(
                session_id, step_dicts, on_failure, _make_job_progress(job), BACKGROUND_BUDGET_S
            )
            response = BatchStepResponse.from_result(result).model_dump(mode="json")
            # Store before raising so a poller sees the partial result.
            job.result = response
            failure = _batch_failure_error(result)
            if failure:
                # Mirror the foreground isError contract: step failures
                # surface as 'failed' job status with the same summary.
                raise ToolError(failure, code="BATCH_STEP_FAILED")
            return response

        batch_id = registry.submit(session_id, len(steps), runner)
        return BatchSubmitResponse(
            action="submitted",
            batch_id=batch_id,
            session_id=session_id,
            total=len(steps),
            estimated_s=round(estimated_s, 2),
            hint=(
                "poll ppsspp_batch_status(batch_id=...) — the batch keeps "
                "running even if this client call timed out; cancel via "
                "ppsspp_batch_cancel(batch_id=...)"
            ),
        ).model_dump(mode="json")

    # ── Foreground (historical contract) ──
    # Budget gate BEFORE any WS work: a >30s foreground batch used to be
    # aborted mid-flight by the MCP client's timeout (remaining steps
    # never ran).
    if estimated_s > FOREGROUND_BUDGET_S:
        raise ToolError(
            f"estimated batch duration {estimated_s:.1f}s exceeds the "
            f"{FOREGROUND_BUDGET_S:.0f}s foreground budget — the MCP client "
            f"aborts tool calls at ~30s and the server-side batch dies with "
            f"it (remaining steps never run). Split the sequence at a safe "
            f"anchor, or re-run with background=true and poll "
            f"ppsspp_batch_status.",
            code="BATCH_BUDGET_EXCEEDED",
        )

    result = await _execute_batch(
        session_id,
        step_dicts,
        on_failure,
        _make_ctx_progress(ctx) if ctx is not None else None,
        FOREGROUND_BUDGET_S,
    )
    response = BatchStepResponse.from_result(result).model_dump(mode="json")
    # A batch whose steps failed must not surface as
    # a plain success — agents rely on isError to notice orchestration
    # failures. Embed a compact per-failure summary instead of the
    # full JSON envelope (screenshots/state-probe payloads made the old
    # details= dump arbitrarily large).
    failure = _batch_failure_error(result)
    if failure:
        raise ToolError(failure, code="BATCH_STEP_FAILED")
    return response


@mcp.tool(
    name="ppsspp_batch_status",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
@translate_tool_errors
async def batch_status(
    batch_id: Annotated[
        str | None,
        Field(
            description=(
                "Job id returned by ppsspp_batch_step(background=true). "
                "Omit to survey ALL retained jobs instead (list mode; "
                "recovers a batch_id after the submit response was lost)."
            ),
        ),
    ] = None,
) -> BatchStatusOutput:
    """PURPOSE: Poll the state and progress of a background batch job without touching the session.

    USAGE: batch_id from ppsspp_batch_step(background=true); omit batch_id for survey/list mode (e.g. to recover a lost batch_id or audit background activity before touching the session).

    BEHAVIOR: Lock-free registry read — never opens the WS transport and never waits for the per-session lock, so it is safe to call while a background batch (or any other tool) owns the session. Executed-step count updates as steps complete; 'completed' carries the full foreground-shaped result. READ-ONLY.

    RETURNS: with batch_id → {batch_id, session_id, status: queued|running|completed|failed|cancelled, executed, total, error, result, retention_jobs}; with batch_id omitted (survey) → {jobs: [{batch_id, session_id, status, executed, total, error, result_present}], retention_jobs} in submission order (finished jobs beyond retention are evicted and absent)."""
    if batch_id is None:
        jobs = get_registry().list_jobs()
        return BatchListResponse(
            jobs=[
                BatchJobSummaryView(
                    batch_id=j.batch_id,
                    session_id=j.session_id,
                    status=j.status,
                    executed=j.executed,
                    total=j.total_steps,
                    error=j.error,
                    result_present=j.result is not None,
                )
                for j in jobs
            ],
            retention_jobs=FINISHED_JOB_RETENTION,
        ).model_dump(mode="json")
    job = get_registry().get(batch_id)
    if job is None:
        raise ToolError(
            f"unknown batch_id {batch_id!r} — never existed, or evicted "
            f"(only the last {FINISHED_JOB_RETENTION} finished jobs are "
            f"retained; queued/running jobs are never evicted)",
            code="BATCH_NOT_FOUND",
        )
    return BatchStatusResponse(
        batch_id=job.batch_id,
        session_id=job.session_id,
        status=job.status,
        executed=job.executed,
        total=job.total_steps,
        error=job.error,
        result=job.result,
        retention_jobs=FINISHED_JOB_RETENTION,
    ).model_dump(mode="json")


@mcp.tool(
    name="ppsspp_batch_cancel",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
@translate_tool_errors
async def batch_cancel(
    batch_id: Annotated[
        str,
        Field(description="Job id returned by ppsspp_batch_step(background=true)."),
    ],
) -> BatchCancelOutput:
    """PURPOSE: Request cancellation of a queued or running background batch job.

    USAGE: batch_id from ppsspp_batch_step(background=true); omit batch_id for survey/list mode (e.g. to recover a lost batch_id or audit background activity before touching the session).

    BEHAVIOR: STATE-CHANGE. Cancels the detached task; the job's own finally block releases the session lock, so subsequent tool calls are free to use the session immediately. The abort happens at the current step boundary (a press finishes, a mid-wait cuts within ~1s). Cancelling an already-finished job is an error — check ppsspp_batch_status first if unsure.

    RETURNS: {batch_id, status, note} — poll ppsspp_batch_status for the terminal state."""
    registry = get_registry()
    try:
        job = registry.cancel(batch_id)
    except KeyError as e:
        raise ToolError(
            f"unknown batch_id {batch_id!r} — never existed, or evicted "
            f"(only the last {FINISHED_JOB_RETENTION} finished jobs are "
            f"retained)",
            code="BATCH_NOT_FOUND",
        ) from e
    except RuntimeError as e:
        raise ToolError(str(e), code="BATCH_ALREADY_FINISHED") from e
    return {
        "batch_id": batch_id,
        "status": job.status,
        "note": (
            "cancellation requested; the job turns 'cancelled' at the "
            "current step boundary — poll ppsspp_batch_status for the "
            "terminal state"
        ),
    }


# ppsspp_batch_list was merged into ppsspp_batch_status(batch_id omitted =
# survey/list mode) in v0.1.6 (Glama surface review: tool-count reduction).
