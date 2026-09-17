"""Health tool wrapper.

1 tool exposed:
- ppsspp_health() — server liveness & readiness probe
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import time
from typing import Any

import pydantic
from mcp.types import ToolAnnotations

from ppsspp_dfx_mcp import __version__
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session import session_manager
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.introspect import HealthResponse

HealthOutput = derive_output_contract("HealthOutput", HealthResponse)

logger = logging.getLogger(__name__)

__all__ = ["health"]

_START_TIME = time.monotonic()


def _probe_sessions_file() -> str | None:
    """Sync probe of sessions.json parse health (worker-thread target).

    Returns an error description string when the file exists but is
    malformed, or None when healthy/absent/empty.
    """
    from ppsspp_dfx_mcp.config import sessions_path

    sp = sessions_path()
    if sp.exists():
        try:
            with sp.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return (
                    "sessions.json root is not a dict — "
                    "file may be corrupted (non-dict root is "
                    "silently reset by _load_sessions)"
                )
        except (OSError, json.JSONDecodeError) as e:
            return (
                f"sessions.json is malformed ({type(e).__name__}) — "
                "parse errors are silently caught by _load_sessions"
            )
    return None


# Former docstring (kept as comment; description is now the TDQS docstring):
# Probe server liveness and readiness.
#
# Returns server version, Python/Pydantic versions, uptime, tool count,
# and active session count. When sessions.json exists but contains no
# sessions (possibly corrupted — _load_sessions catches parse errors
# and returns empty dict), returns status="degraded" with
# session_error set, so callers can distinguish "no active sessions"
# from "sessions.json may be broken".
@mcp.tool(
    name="ppsspp_health",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
@translate_tool_errors
async def health() -> HealthOutput:
    """PURPOSE: Probe MCP server liveness and readiness without contacting PPSSPP. Use this before any session-dependent tool to verify the server is up.

    USAGE: No parameters.

    BEHAVIOR: READ-ONLY. Reads in-memory server counters (uptime, registered tool count, active session count). Does not contact PPSSPP and does not modify any state.

    RETURNS: Dict with status ('ok'/'degraded'), version, python_version, pydantic_version, uptime_s, tool_count, session_count.
    """
    logger.info("tool_call", extra={"tool": "ppsspp_health"})
    sessions: list[Any] = []
    session_error: str | None = None
    try:
        sessions = await session_manager.list_sessions()
    except Exception as e:
        # Unexpected error from list_sessions (should not happen —
        # _load_sessions catches all I/O/parse errors internally, but
        # defensive: surface any surprise exceptions as degraded status).
        logger.warning("list_sessions unexpected error: %s", e)
        sessions = []
        session_error = f"{type(e).__name__}: {e}"
    else:
        # _load_sessions silently catches (OSError, json.JSONDecodeError)
        # and returns empty dict, masking corruption as "0 sessions".
        # Probe the file directly with the same parse contract as
        # _load_sessions: only a real parse failure (malformed JSON or a
        # non-dict root) is degraded. A well-formed but empty `{}` file is
        # the normal "all sessions stopped" residue — status stays 'ok'.
        # The probe does blocking file I/O — run it in a worker
        # thread so the health tool never stalls the event loop.
        if not sessions:
            session_error = await asyncio.to_thread(_probe_sessions_file)
    # Read the live registration count from the server's tool registry
    # (static tools registered via decorators at import, dynamic exposed
    # scripts via add_tool in lifespan).
    from ppsspp_dfx_mcp.server import registered_tool_count

    tool_count = registered_tool_count()
    response = HealthResponse(
        status="degraded" if session_error is not None else "ok",
        version=__version__,
        python_version=platform.python_version(),
        pydantic_version=pydantic.__version__,
        uptime_s=time.monotonic() - _START_TIME,
        tool_count=tool_count,
        session_count=len(sessions),
        session_error=session_error,
    )
    return response.model_dump(mode="json")
