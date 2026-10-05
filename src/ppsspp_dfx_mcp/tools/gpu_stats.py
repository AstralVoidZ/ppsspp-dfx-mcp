"""gpu_stats: attach a timeout cause to the error.

This failure was diagnosed twice and got it wrong twice, because "gpu.stats.get
timed out" is compatible with four different situations (see
core/call_attribution.py). The tool reported only the symptom, so the
caller had to guess.

Now a WS_TIMEOUT from this tool carries the attributed cause and what to do
about it, read from the transport's own CallDiagnostics. The
diagnostics are fetched at failure time -- the record is already settled by
then, so no new plumbing is needed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.core.call_attribution import (
    CAUSE_NO_PRODUCER,
    CAUSE_UNKNOWN,
    describe,
    hint_for,
)
from ppsspp_dfx_mcp.errors import ToolError, WsTimeout
from ppsspp_dfx_mcp.models.gpu_stats import GpuStatsResult
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import require_session_id, translate_tool_errors
from ppsspp_dfx_mcp.views.gpu_stats import GpuStatsResponse

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    GpuStatsOutput = dict[str, Any]
else:
    GpuStatsOutput = derive_output_contract("GpuStatsOutput", GpuStatsResponse)

logger = logging.getLogger(__name__)

__all__ = ["gpu_stats"]


# The event whose diagnostics we read on failure.
_TARGET_EVENT = "gpu.stats.get"

# to_tool_error re-classifies a ticketed timeout as this when the PID is alive
# and the game is running. It is a *ToolError*, not a WsTimeout, which is why
# the attribution arm below must also handle it.
_FREEZE_SUSPECTED_CODE = "CPU_FREEZE_SUSPECTED"

# G-7 (FR-007): reused from the attribution table so the "not rendering"
# re-classification carries the same next-step advice as a diagnosed
# no_producer timeout, instead of inventing a second wording.
_HINT_NO_PRODUCER = hint_for(CAUSE_NO_PRODUCER)


def _rendering_state(client: object) -> bool | None:
    """Whether the session is producing frames — True/False/None.

    Tri-state on purpose: None means "cannot tell" (no observer, feed never
    started, or a client without the attribute), and the caller MUST keep
    the existing behavior there. Treating unknown as "not rendering" would
    relabel every ordinary CPU freeze as a GPU problem.

    Defensive like ``_attribution_suffix``: this runs while handling a
    failure, so it must not raise.
    """
    try:
        observer = getattr(client, "_game_state_observer", None)
        if observer is None:
            return None
        return observer.is_rendering()
    except Exception:  # noqa: BLE001 - must never mask the error it explains
        return None


def _attribution_suffix(client: object) -> str:
    """Render the attributed cause of the last _TARGET_EVENT timeout.

    Returns '' when there is nothing usable to say, so the original error
    text is never buried under an empty explanation.

    Every step is defensive: this runs while handling a failure, so it must
    not raise. A diagnostics object that explodes on attribute access (or a
    transport with no such attribute) yields '' rather than replacing the
    timeout with an unrelated error.
    """
    try:
        diagnostics = getattr(getattr(client, "_transport", None), "diagnostics", None)
        if diagnostics is None:
            return ""
        record = diagnostics.last(_TARGET_EVENT)
    except Exception:  # noqa: BLE001 - must never mask the error it explains
        return ""
    if not record:
        return ""
    try:
        info = describe(record)
    except Exception:  # noqa: BLE001
        return ""
    if info["cause"] == CAUSE_UNKNOWN:
        return ""
    ev = info["evidence"]
    return (
        f"\n  cause: {info['cause']}"
        f"\n  saw_same_event_broadcast: {ev['saw_same_event_broadcast']}"
        f"\n  stepping_at_timeout: {ev['stepping_at_timeout']}"
        f"\n  next: {info['hint']}"
    )


@mcp.tool(
    name="ppsspp_gpu_stats",
    annotations=ToolAnnotations(
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    ),
)
@translate_tool_errors
async def gpu_stats(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
) -> GpuStatsOutput:
    """PURPOSE: Query GPU counters — fps, vblanks per second, timing info.

    USAGE: session_id. The CPU must be RUNNING; paused, the MCP pre-probe returns CPU_STATE_ERROR instead of hanging — which doubles as the cheapest paused-CPU probe.

    BEHAVIOR: READ-ONLY.

    ON TIMEOUT: the error names WHY it timed out -- expected_stall (the CPU was stepping, so no frame
    is coming), pairing_broken (a broadcast arrived meanwhile, so ticket pairing failed), or no_producer
    (nothing was broadcast at all, so the emulator is not producing frames -- check for a modal dialog
    blocking it). A CPU_FREEZE_SUSPECTED is re-checked against the frame heartbeat first: if no frames
    are arriving it is reported as WS_TIMEOUT with a no-producer attribution, NOT as a CPU freeze.

    RETURNS: {fps, vblanks_per_second, info, timing, raw, text}."""
    require_session_id(session_id)

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_gpu_stats",
            "session_id": session_id,
        },
    )

    client_obj: object | None = None
    try:
        async with session_client(session_id) as client:
            client_obj = client
            raw = await client.gpu_stats()
    except WsTimeout as e:
        # A bare "timed out" is what made this failure undiagnosable.
        suffix = _attribution_suffix(client_obj)
        # ToolError.__str__ already renders "[CODE] message"; re-wrapping
        # with str(e) used to produce "[WS_TIMEOUT] [WS_TIMEOUT] timed out".
        raw_text = e.args[0] if e.args else str(e)
        raise WsTimeout(f"{raw_text}{suffix}") from e
    except ToolError as e:
        # Measured on a real session, 2026-10-02: a genuine
        # gpu.stats timeout is re-classified by to_tool_error as
        # CPU_FREEZE_SUSPECTED ("PID alive AND game running"), so it never
        # matched the `except WsTimeout` arm above -- and re-raising it
        # untouched meant the three-way attribution NEVER RAN on the one
        # failure it exists to explain. The tool then reported a CPU freeze
        # while a screenshot taken in the same second showed a 59.4 fps
        # render (mcp_test_report/gpu_channel_root_cause.md section 3).
        if getattr(e, "code", "") == _FREEZE_SUSPECTED_CODE:
            suffix = _attribution_suffix(client_obj)
            # G-7 (FR-007): CPU_FREEZE_SUSPECTED is derived from "PID alive +
            # game running", which says the CPU executes -- NOT that anything
            # is being drawn. A non-rendering game (black screen, modal
            # dialog, GPU pipeline stall) is still "running", so this code
            # sent the caller after a CPU death-loop that cannot be the
            # cause. Check the frame heartbeat before believing it.
            rendering = _rendering_state(client_obj)
            if rendering is False:
                raise WsTimeout(
                    f"{e.args[0] if e.args else str(e)} — Hint: timed out, "
                    f"PID alive + game running but NO frames are being "
                    f"produced, so this is NOT a CPU freeze: the renderer or "
                    f"GPU producer is stalled (check for a modal dialog, a "
                    f"black screen, or a GPU pipeline stall). "
                    f"{_HINT_NO_PRODUCER}"
                ) from e
            if suffix:
                # Rebuild from the RAW message: ToolError.__str__ already
                # prefixes "[CODE] ", so passing str(e) would double it.
                raw_text = e.args[0] if e.args else str(e)
                raise type(e)(f"{raw_text}{suffix}") from e
        raise

    if not isinstance(raw, dict):
        raw = {}

    result = GpuStatsResult.from_raw(raw)
    return GpuStatsResponse.from_result(result).model_dump(mode="json")
