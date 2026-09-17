"""Input tool wrappers.

4 tools exposed:
- ppsspp_press_button(session_id, button, duration?) — simulate button press
- ppsspp_hold_buttons(session_id, buttons) — hold button combination
- ppsspp_send_analog(session_id, x, y) — send analog stick position
- ppsspp_wait_frames(session_id, frames, interval?) — wait N frames

Async: uses session_client → PpssppDebugClient (composes WsTransport
+ SteppingManager) under the hood. Tools call DebugClient domain methods
(press_button / hold_buttons / send_analog) directly; no orchestration
wrapper indirection. wait_frames uses validate_session_alive (sync) and
does not open a WebSocket connection.
"""

from __future__ import annotations

import logging
from typing import Annotated

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.core.primitives import MAX_PRESS_DURATION_FRAMES
from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError, to_tool_error
from ppsspp_dfx_mcp.models.input import PPSSPP_ALL_BUTTONS as _PPSSPP_ALL_BUTTONS
from ppsspp_dfx_mcp.models.input import (
    ButtonPressResult,
    HoldButtonsResult,
    SendAnalogResult,
    WaitFramesResult,
)
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.tools._common import translate_tool_errors, wait_frames_chunked
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.input import (
    ButtonPressResponse,
    HoldButtonsResponse,
    SendAnalogResponse,
    WaitFramesResponse,
)

ButtonPressOutput = derive_output_contract("ButtonPressOutput", ButtonPressResponse)
HoldButtonsOutput = derive_output_contract("HoldButtonsOutput", HoldButtonsResponse)
SendAnalogOutput = derive_output_contract("SendAnalogOutput", SendAnalogResponse)
WaitFramesOutput = derive_output_contract("WaitFramesOutput", WaitFramesResponse)

logger = logging.getLogger(__name__)

__all__ = ["press_button", "hold_buttons", "send_analog", "wait_frames"]

# PSP runs at ~60 FPS for most games; PPSSPP defaults to 60. The shared
# pacing constants and the chunked+liveness waiter live in tools/_common
# so batch_step's wait steps share the exact same semantics.

# Button names accepted by PPSSPP's WebSocket debugger — canonical tuple
# lives in models/input.py (PPSSPP_ALL_BUTTONS, wire-contract layer), so
# input tools and batch steps validate against one vocabulary.

# Tool-facing alias set: the full PPSSPP table plus common alternate
# spellings callers may use.
_VALID_BUTTONS = _PPSSPP_ALL_BUTTONS


def _validate_buttons_combo(buttons: str) -> None:
    """Validate a 'cross|circle'-style button combination string.

    Raises ToolError if empty or any token is not a known button name.
    """
    if not buttons or not buttons.strip():
        raise ArgsInvalid("buttons must be a non-empty string")
    tokens = [t.strip() for t in buttons.split("|") if t.strip()]
    if not tokens:
        raise ArgsInvalid("buttons must contain at least one button name")
    invalid = [t for t in tokens if t not in _VALID_BUTTONS]
    if invalid:
        raise ArgsInvalid(f"invalid button(s) {invalid!r}; expected each in {_VALID_BUTTONS}")


# Former docstring (kept as comment; description is now the TDQS docstring):
# Simulate a PSP button press.
#
# Returns:
# ButtonPressResponse dict: button + duration.
#
# Raises:
# ToolError: on session lookup failure, WS failure, or invalid button.
@mcp.tool(
    name="ppsspp_press_button",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    ),
)
@translate_tool_errors
async def press_button(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    button: Annotated[
        str,
        Field(
            description=(
                "Button name. Valid: cross / circle / triangle / square / "
                "up / down / left / right / start / select / ltrigger / rtrigger."
            ),
        ),
    ],
    duration: Annotated[
        int,
        Field(default=1, description="Press duration in frames (default 1)."),
    ] = 1,
) -> ButtonPressOutput:
    """PURPOSE: Simulate a single PSP button press for a duration.

    USAGE: session_id + button required; duration optional (default 10 frames). Valid button names: cross / circle / triangle / square / up / down / left / right / start / select / ltrigger / rtrigger.

    BEHAVIOR: STATE-CHANGE. Sends input events to PPSSPP. Button state returns to released after the duration elapses.

    RETURNS: {button, duration}.
    """
    if button not in _VALID_BUTTONS:
        raise ArgsInvalid(f"invalid button={button!r}; expected one of {_VALID_BUTTONS}")
    if duration < 0:
        raise ArgsInvalid(f"duration must be >= 0; got {duration}")
    if duration > MAX_PRESS_DURATION_FRAMES:
        # The WS ticket timeout scales with duration/60*1.5 — an
        # unbounded duration meant an unbounded tool hang.
        raise ArgsInvalid(
            f"duration {duration} exceeds the cap {MAX_PRESS_DURATION_FRAMES} "
            f"(~{MAX_PRESS_DURATION_FRAMES // 60}s of hold time)"
        )
    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_press_button",
            "session_id": session_id,
            "button": button,
            "duration": duration,
        },
    )
    try:
        async with session_client(session_id) as client:
            await client.press_button(button=button, duration=duration)
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e
    result = ButtonPressResult(button=button, duration=duration)
    return ButtonPressResponse.from_result(result).model_dump(mode="json")


# Former docstring (kept as comment; description is now the TDQS docstring):
# Hold a combination of PSP buttons.
#
# STATE-CHANGE: changes the persistent button state. The buttons remain
# held until a subsequent call changes the state.
#
# Returns:
# HoldButtonsResponse dict: buttons.
#
# Raises:
# ToolError: on session lookup failure, WS failure, or invalid button.
@mcp.tool(
    name="ppsspp_hold_buttons",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    ),
)
@translate_tool_errors
async def hold_buttons(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    buttons: Annotated[
        str,
        Field(
            description=(
                "Button combination string. Multiple buttons separated by "
                "'|' (e.g. 'cross|circle'). Held until released (send an "
                "empty combination or use press_button to clear). Valid "
                "names: cross / circle / triangle / square / up / down / "
                "left / right / start / select / ltrigger / rtrigger."
            ),
        ),
    ],
) -> HoldButtonsOutput:
    """PURPOSE: Hold a combination of PSP buttons until a subsequent call changes the state.

    USAGE: session_id + buttons required (pipe-separated combination, e.g. 'cross|circle'). Valid names: cross / circle / triangle / square / up / down / left / right / start / select / ltrigger / rtrigger.

    BEHAVIOR: STATE-CHANGE. Sets button-held state in PPSSPP; persists until next hold_buttons / send_analog call.

    RETURNS: {buttons}.
    """
    # Empty string = release all held buttons (per Field description).
    # Only validate non-empty combinations to allow the release path.
    if buttons and buttons.strip():
        _validate_buttons_combo(buttons)
    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_hold_buttons",
            "session_id": session_id,
            "buttons": buttons,
        },
    )
    # Parse "cross|circle" → {"cross": True, "circle": True} for PPSSPP.
    # PPSSPP's input.buttons.send requires an object mapping button name →
    # bool, and only SETS the keys present in the object — an empty object
    # is a no-op. The documented empty-string "release all" behaviour is
    # therefore implemented by explicitly sending false for every button
    # in the PPSSPP lookup table (aligned with InputSubscriber.cpp
    # buttonLookup, 25 entries).
    tokens = [t.strip() for t in buttons.split("|") if t.strip()]
    if tokens:
        buttons_dict = {t: True for t in tokens}
    else:
        buttons_dict = {name: False for name in _PPSSPP_ALL_BUTTONS}
    try:
        async with session_client(session_id) as client:
            await client.hold_buttons(buttons=buttons_dict)
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e
    result = HoldButtonsResult(buttons=buttons)
    return HoldButtonsResponse.from_result(result).model_dump(mode="json")


# Former docstring (kept as comment; description is now the TDQS docstring):
# Send an analog stick position.
#
# STATE-CHANGE: changes the persistent analog stick state. The stick
# remains at the specified position until a subsequent call changes it.
#
# Returns:
# SendAnalogResponse dict: x + y.
#
# Raises:
# ToolError: on session lookup failure, WS failure, or out-of-range.
@mcp.tool(
    name="ppsspp_send_analog",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    ),
)
@translate_tool_errors
async def send_analog(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    x: Annotated[
        int,
        Field(
            description=(
                "Analog X coordinate in [0, 255] (128 = center). 0 = full left, 255 = full right."
            ),
        ),
    ],
    y: Annotated[
        int,
        Field(
            description=(
                "Analog Y coordinate in [0, 255] (128 = center). 0 = full up, 255 = full down."
            ),
        ),
    ],
) -> SendAnalogOutput:
    """PURPOSE: Send an analog stick position (x, y in [0, 255], 128 = center).

    USAGE: session_id + x + y required. 0 = full left / up, 255 = full right / down.

    BEHAVIOR: STATE-CHANGE. Sets analog stick position; persists until next send_analog call.

    RETURNS: {x, y}.
    """
    if not 0 <= x <= 255:
        raise ArgsInvalid(f"x must be in [0, 255]; got {x}")
    if not 0 <= y <= 255:
        raise ArgsInvalid(f"y must be in [0, 255]; got {y}")
    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_send_analog",
            "session_id": session_id,
            "x": x,
            "y": y,
        },
    )
    # PPSSPP's input.analog.send expects floats in [-1.0, 1.0].
    # Convert PSP-native [0, 255] (128 = center) → normalized [-1.0, 1.0].
    x_norm = (x - 128) / 128.0
    y_norm = (y - 128) / 128.0
    try:
        async with session_client(session_id) as client:
            await client.send_analog(x=x_norm, y=y_norm)
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e
    result = SendAnalogResult(x=x, y=y)
    return SendAnalogResponse.from_result(result).model_dump(mode="json")


# Former docstring (kept as comment; description is now the TDQS docstring):
# Wait for N frames (wall-clock sleep; no PPSSPP interaction beyond liveness check).
#
# Implementation: validates the session is alive via validate_session_alive,
# then sleeps for `frames * interval` seconds. The session_id parameter
# exists so callers explicitly scope the wait to a session (and fail fast
# if the session died). For long waits the sleep is chunked (~1s) and the
# session is re-validated between chunks so a session death mid-wait is
# surfaced promptly.
#
# Returns:
# WaitFramesResponse dict: frames + elapsed_s.
#
# Raises:
# ToolError: on session lookup failure.
@mcp.tool(
    name="ppsspp_wait_frames",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
@translate_tool_errors
async def wait_frames(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    frames: Annotated[
        int,
        Field(description="Number of frames to wait (at 60 FPS, frames/60 seconds)."),
    ],
    interval: Annotated[
        float | None,
        Field(
            default=None,
            description=(
                "Per-frame interval in seconds (default 1/60). Total wait = frames * interval."
            ),
        ),
    ] = None,
) -> WaitFramesOutput:
    """PURPOSE: Wait N frames (wall-clock sleep at 60 FPS by default) to let the emulator advance.

    USAGE: session_id + frames required; interval optional (default 1/60 s).

    BEHAVIOR: STATE-CHANGE. Sleeps the caller; emulator advances N frames. Session must be alive (validated before sleep).

    RETURNS: {frames, elapsed_s}.
    """
    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_wait_frames",
            "session_id": session_id,
            "frames": frames,
            "interval": interval,
        },
    )
    # Shared chunked waiter — validates frames/interval
    # (interval <= 0 used to become a hot loop hammering sessions.json),
    # caps frames, and validates session liveness before/between chunks.
    try:
        elapsed = await wait_frames_chunked(frames, interval, session_id)
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e
    result = WaitFramesResult(frames=frames, elapsed_s=elapsed)
    return WaitFramesResponse.from_result(result).model_dump(mode="json")
