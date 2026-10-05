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
from typing import TYPE_CHECKING, Annotated, Any, TypedDict

import pydantic
from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp import __version__
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.session import session_manager
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views.introspect import HealthResponse

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    HealthOutput = dict[str, Any]
else:
    HealthOutput = derive_output_contract("HealthOutput", HealthResponse)


class _HealthSessionCheck(TypedDict, total=False):
    name: str
    passed: bool
    detail: str
    # How the value came to be, so a failed probe is never mistaken for a
    # probe that read zero. `value` is present only when value_status is
    # "ok" -- a failed read carries no value at all.
    value_status: str
    value: int


# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    HealthWithSessionChecks = dict[str, Any]
else:

    class HealthWithSessionChecks(HealthOutput, total=False):
        """HealthOutput + the per-session battery keys added when `session_id`
        is given. They previously existed only in the text channel — the
        output contract dropped them from structuredContent."""

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


@mcp.tool(
    name="ppsspp_health",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
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

    READING session_checks: each entry carries `value_status` besides `passed` -- 'ok' (the probe really read), 'stale_address_suspected' (the read succeeded and returned zero on several consecutive readings, so the probe address may have drifted -- a suspicion, not a verdict), 'failed' (the read raised or the data was absent; no value is reported), 'not_configured' (no probe address, so nothing was read). A probe that READ ZERO and one that COULD NOT READ both show passed=false while meaning opposite things: the first is a fact about the game, the second about the tooling. Do not read passed=false alone as a finding about the emulated game.

    RETURNS: Dict with status ('ok'/'degraded'), version, python_version, pydantic_version, uptime_s, tool_count, session_count — plus session_checks: [{name, passed, detail, value_status, value?}] and overall_session_status when session_id is provided.
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
    from ppsspp_dfx_mcp.registry import registered_tool_count

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
        # a stubbed/short-circuit battery may return None; the summary below
        # iterates it, so normalise once here rather than at each use.
        checks = list(checks or [])
        out["session_checks"] = checks
        out["overall_session_status"] = overall

        # The headline must not read "ok" while this same
        # response reports a failed battery.
        if overall != "pass":
            out["status"] = "degraded"
            failed = [str(c.get("name")) for c in checks if not c.get("passed")]
            # Name the failing checks up top so the reason is
            # readable without walking the detail list.
            out["failed_session_checks"] = failed
            if failed:
                out["session_error"] = f"session battery failed ({overall}): " + ", ".join(failed)
    return out
