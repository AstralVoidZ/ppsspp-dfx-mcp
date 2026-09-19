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
from typing import Annotated, Any, TypedDict

import pydantic
from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp import __version__
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session import session_manager
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.introspect import HealthResponse

HealthOutput = derive_output_contract("HealthOutput", HealthResponse)


class _HealthSessionCheck(TypedDict, total=False):
    name: str
    passed: bool
    detail: str


class HealthWithSessionChecks(HealthOutput, total=False):
    """HealthOutput + the per-session battery keys added when `session_id`
    is given (M9: they previously existed only in the text channel — the
    output contract dropped them from structuredContent)."""

    session_checks: list[_HealthSessionCheck]
    overall_session_status: str


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
async def health(
    session_id: Annotated[
        str | None,
        Field(
            description=(
                "Optional session ID — when provided, appends "
                "session_checks (the four-point battery: iso_loaded / "
                "cpu_running / ws_connected / game_mode_valid, absorbed "
                "from ppsspp_smoke_test) to the server-level report. "
                "Omit for the zero-contact server liveness probe."
            ),
        ),
    ] = None,
) -> HealthWithSessionChecks:
    """PURPOSE: Probe MCP server liveness and readiness — plus an optional four-point session health battery.

    USAGE: no args for the server-level probe (does NOT contact PPSSPP); pass session_id to also run the session battery (iso_loaded / cpu_running / ws_connected / game_mode_valid — absorbed from the former ppsspp_smoke_test tool).

    BEHAVIOR: READ-ONLY. Server counters are read in-memory; the session battery (when requested) contacts PPSSPP over the session transport but never mutates state.

    RETURNS: Dict with status ('ok'/'degraded'), version, python_version, pydantic_version, uptime_s, tool_count, session_count — plus session_checks: [{name, passed, detail}] and overall_session_status when session_id is provided.
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
    out = response.model_dump(mode="json")
    if session_id:
        from ppsspp_dfx_mcp.tools.smoke import run_smoke_checks

        checks, overall = await run_smoke_checks(session_id, checks=None)
        out["session_checks"] = checks
        out["overall_session_status"] = overall
    return out
