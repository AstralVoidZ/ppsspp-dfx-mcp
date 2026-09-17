"""Step tool wrapper.

1 tool exposed:
- ppsspp_step(action, ...) — aggregate CPU stepping + run-state control

Actions:
- 'into' — step into (including delay slot)
- 'over' — step over (skip function calls)
- 'out' — step out of current function
- 'pause' — pause CPU (enter stepping mode)
- 'resume' — resume CPU (exit stepping mode)
- 'reset' — reset the game (reboot)
- 'run_until' — run until the specified address is reached (requires address)
- 'next_hle' — step to next HLE callback

Split from `tools/breakpoint.py` (task 7.3). The original step() function
only supported into / over / resume; this rewrite adds out / pause / reset
/ run_until / next_hle to cover the full SteppingManager + DebugClient
domain API.

Async: uses session_client → PpssppDebugClient (composes WsTransport
+ SteppingManager) under the hood. Tools call DebugClient domain methods
(step_into / step_over / step_out / pause / resume / reset / run_until /
next_hle) directly; no orchestration wrapper indirection.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError, to_tool_error
from ppsspp_dfx_mcp.models.step import StepResult
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import resolve_session_id, session_client
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.step import StepResponse

StepOutput = derive_output_contract("StepOutput", StepResponse)

logger = logging.getLogger(__name__)

__all__ = ["step"]

_STEP_ACTIONS: tuple[str, ...] = (
    "into",
    "over",
    "out",
    "pause",
    "resume",
    "reset",
    "run_until",
    "next_hle",
)


def _extract_broadcast_fields(resp: dict[str, Any] | None) -> dict[str, Any]:
    """Extract pc/ticks/reason/related_address from a cpu.stepping broadcast.

    The 5 stepping actions (into/over/out/run_until/next_hle) return a
    cpu.stepping broadcast dict from _confirm_step_completed(). This helper
    safely extracts the 4 broadcast fields, returning empty defaults if the
    response is None or missing keys (e.g. legacy fallback path returns
    cpu.status which has no broadcast fields).

    Returns a dict suitable for **-unpacking into StepResult().
    """
    if not isinstance(resp, dict):
        return {}
    return {
        "pc": int(resp.get("pc", 0)),
        "ticks": float(resp.get("ticks", 0.0)),
        "reason": str(resp.get("reason", "")),
        "related_address": int(resp.get("relatedAddress", 0)),
    }


# Former docstring (kept as comment; description is now the TDQS docstring):
# Aggregate CPU step / run-state tool.
#
# Action → required params:
# into      → session_id
# over      → session_id
# out       → session_id
# pause     → session_id
# resume    → session_id
# reset     → session_id
# run_until → session_id + address
# next_hle  → session_id
@mcp.tool(
    name="ppsspp_step",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    ),
)
@translate_tool_errors
async def step(
    action: Annotated[
        Literal[
            "into",
            "over",
            "out",
            "pause",
            "resume",
            "reset",
            "run_until",
            "next_hle",
        ],
        Field(
            description=(
                "CPU step / run-state operation. Valid values:\n"
                "- 'into': step into (including delay slot).\n"
                "- 'over': step over (skip function calls).\n"
                "- 'out': step out of current function.\n"
                "- 'pause': pause CPU (enter stepping mode).\n"
                "- 'resume': resume CPU (exit stepping mode).\n"
                "- 'reset': reset the game (reboot).\n"
                "- 'run_until': run until the specified address is reached "
                "(requires address).\n"
                "- 'next_hle': step to next HLE callback."
            ),
        ),
    ],
    address: Annotated[
        str,
        Field(
            default="0x0",
            description=(
                "Target address, as a hex string (e.g. '0x08804000'). "
                "Required for action='run_until'; "
                "ignored for all other actions."
            ),
        ),
    ] = "0x0",
    session_id: Annotated[
        str | None,
        Field(
            description=(
                "Active session ID; omit to auto-resolve when exactly one session is active."
            ),
        ),
    ] = None,
) -> StepOutput:
    """PURPOSE: Aggregate CPU step + run-state control (into / over / out, pause, resume, reset, run_until, next_hle).

    USAGE: action='into' / 'over' / 'out' / 'pause' / 'resume' / 'reset' / 'next_hle' take only session_id (optional when exactly one session is active); 'run_until' requires address.


    ROUTING: single CPU-step operations -> here (run_until for run-to-address); multi-step press/wait/probe sequences -> ppsspp_batch_step.
    BEHAVIOR: STATE-CHANGE. Advances or changes CPU run state. 'reset' reboots the game (lost in-memory state). 'run_until' sets a temp breakpoint and resumes.

    RETURNS: {action, address}.
    """
    session_id = await resolve_session_id(session_id)
    if action not in _STEP_ACTIONS:
        raise ArgsInvalid(f"invalid action={action!r}; expected one of {_STEP_ACTIONS}")
    address_int = parse_address(address)
    if action == "run_until" and address_int == 0:
        raise ArgsInvalid("address is required when action=run_until")

    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_step", "action": action, "session_id": session_id},
    )

    try:
        async with session_client(session_id) as client:
            if action == "into":
                resp = await client.step_into()
                result = StepResult(action=action, **_extract_broadcast_fields(resp))
            elif action == "over":
                resp = await client.step_over()
                result = StepResult(action=action, **_extract_broadcast_fields(resp))
            elif action == "out":
                resp = await client.step_out()
                result = StepResult(action=action, **_extract_broadcast_fields(resp))
            elif action == "pause":
                await client.pause()
                # After pause, CPU is stepping → PC is trustworthy.
                # Use safe_get_pc which guarantees stepping state during
                # the query (handles VBlank race where CPU auto-resumes
                # between pause() and get_pc()).
                pc, _trust = await client.safe_get_pc()
                result = StepResult(action=action, pc=pc)
            elif action == "resume":
                await client.resume()
                result = StepResult(action=action)
            elif action == "reset":
                await client.reset()
                result = StepResult(action=action)
            elif action == "run_until":
                resp = await client.run_until(address=address_int)
                result = StepResult(
                    action=action, address=address_int, **_extract_broadcast_fields(resp)
                )
            else:  # next_hle
                resp = await client.next_hle()
                result = StepResult(action=action, **_extract_broadcast_fields(resp))
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e
    return StepResponse.from_result(result).model_dump(mode="json")
