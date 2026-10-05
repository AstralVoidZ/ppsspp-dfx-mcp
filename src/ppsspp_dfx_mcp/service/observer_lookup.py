"""Live-observer lookup for the wait/trace tools (W19).

``_get_live_observer`` used to live in ``tools/workflows.py`` and was
imported from there by ``tools/breakpoint.py``. Moving it into the service
layer keeps the tool modules from reaching into each other and gives the
"no live broadcast observer" error a single home.
"""

from __future__ import annotations

from typing import Any

from ppsspp_dfx_mcp.errors import ArgsInvalid, SessionNotFound
from ppsspp_dfx_mcp.session import session_manager

__all__ = ["get_live_observer"]


async def get_live_observer(session_id: str) -> Any:
    """Return the session's GameStateObserver (arming prerequisite).

    Raises a ToolError (not SessionNotFound) when the session has no
    live observer — fake-mode sessions and disk-loaded sessions have
    none, and the message should say that instead of implying the
    session is gone.
    """
    try:
        return await session_manager.get_observer(session_id)
    except SessionNotFound as e:
        raise ArgsInvalid(
            f"session {session_id} has no live broadcast observer — "
            f"wait/trace tools (ppsspp_breakpoint(action='wait') / "
            f"ppsspp_breakpoint(action='trace') / ppsspp_wait_frames) "
            f"require a "
            f"live PPSSPP WebSocket link; fake-mode and disk-loaded "
            f"sessions have none. Recovery: check "
            f"ppsspp_health(session_id=...) (ws_connected), then start a "
            f"fresh session via "
            f"ppsspp_session(action='start') and retry."
        ) from e
