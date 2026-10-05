"""Read-only snapshot Resources (MCP spec context data).

Implements the delivery U-02 decision: `ppsspp://game-state` and
`ppsspp://registers` expose live session snapshots as Resources, so an
Agent can pull context without spending a tool call. Both are READ-ONLY
probes of the active PPSSPP session.

Session-selection convention: these URIs are session-less (a static
Resource cannot take arguments), so both require exactly ONE active
session. The "exactly one" policy is delegated to the tool-layer helper
``client_helper.resolve_session_id`` (single source of truth); its
``SessionNotFound`` / ``SessionAmbiguous`` outcomes are translated into
``ResourceError`` here, because ``server.py`` only classifies
``ResourceError`` as an anticipated resource failure (a raw business
exception would degrade the read into a crash with a traceback).

Registered via `@mcp.resource()` decorator (imported by
`server.register_all_tools()`); requires the SDK v2 resource support.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver.exceptions import ResourceError

from ppsspp_dfx_mcp.errors import SessionAmbiguous, SessionNotFound, to_tool_error
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.session import session_manager
from ppsspp_dfx_mcp.session.client_helper import resolve_session_id, session_client

logger = logging.getLogger(__name__)

__all__ = ["game_state", "registers"]


async def _require_single_session() -> str:
    """Return the session_id of the single active session, or raise.

    Resources cannot take a session argument (static URIs), so the
    snapshot only makes sense when exactly one session is active. The
    0 / 1 / 2+ resolution is delegated to ``resolve_session_id`` (the same
    rule the session-argument tools use); its two business exceptions are
    translated to ``ResourceError`` — the type ``server.py`` classifies.
    The zero-session text is pinned by
    ``tests/mcp_inspector/test_resources_prompts.py``.
    """
    try:
        return await resolve_session_id(None)
    except SessionNotFound as e:
        raise ResourceError(
            "no active PPSSPP session — start one with ppsspp_session(action=start, iso_path=...)"
        ) from e
    except SessionAmbiguous as e:
        # Use the raw message (args[0]); ToolError.__str__ would prepend the
        # [SESSION_AMBIGUOUS] code prefix, which does not belong inside a
        # ResourceError message.
        raise ResourceError(
            f"{e.args[0]} — snapshot resources require exactly one active "
            'session; use ppsspp_session(action="list") and the session-argument '
            "tools (ppsspp_query / ppsspp_read_memory) instead"
        ) from e


@mcp.resource(
    "ppsspp://game-state",
    name="game-state",
    description=(
        "READ-ONLY snapshot of the single active PPSSPP session's game "
        "status (running/paused state and game title via the game.status "
        "event). Requires exactly one active session."
    ),
)
async def game_state() -> dict[str, Any]:
    """Snapshot the active session's game status."""
    session_id = await _require_single_session()
    logger.info("resource_read", extra={"resource": "ppsspp://game-state"})
    try:
        async with session_client(session_id) as client:
            status = await client.game_status()
    except Exception as e:
        raise to_tool_error(e) from e
    sess = await session_manager.get_session_state(session_id)
    return {"session_id": session_id, "restored": bool(sess.extra.get("restored")), "game": status}


@mcp.resource(
    "ppsspp://registers",
    name="registers",
    description=(
        "READ-ONLY snapshot of all CPU registers (GPR + FPU + VFPU via "
        "cpu.getAllRegs) for the single active PPSSPP session. Requires "
        "exactly one active session."
    ),
)
async def registers() -> dict[str, Any]:
    """Snapshot all CPU registers of the active session."""
    session_id = await _require_single_session()
    logger.info("resource_read", extra={"resource": "ppsspp://registers"})
    try:
        async with session_client(session_id) as client:
            regs = await client.get_all_regs()
    except Exception as e:
        raise to_tool_error(e) from e
    sess = await session_manager.get_session_state(session_id)
    return {
        "session_id": session_id,
        "restored": bool(sess.extra.get("restored")),
        "registers": regs,
    }
