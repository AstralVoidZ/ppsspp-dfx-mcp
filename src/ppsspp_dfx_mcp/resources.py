"""Read-only snapshot Resources (MCP spec context data).

Implements the delivery U-02 decision: `ppsspp://game-state` and
`ppsspp://registers` expose live session snapshots as Resources, so an
Agent can pull context without spending a tool call. Both are READ-ONLY
probes of the active PPSSPP session.

Session-selection convention: these URIs are session-less (a static
Resource cannot take arguments), so both require exactly ONE active
session. Zero sessions → ResourceError listing the prerequisite; more
than one → ResourceError directing the caller to `ppsspp_session_list`
+ the session-argument tools (`ppsspp_query`).

Registered via `@mcp.resource()` decorator (imported by
`server.register_all_tools()`); requires the SDK v2 resource support.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver.exceptions import ResourceError

from ppsspp_dfx_mcp.errors import to_tool_error
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client_with_transport
from ppsspp_dfx_mcp.session import session_manager

logger = logging.getLogger(__name__)

__all__ = ["game_state", "registers"]


async def _require_single_session() -> str:
    """Return the session_id of the single active session, or raise.

    Resources cannot take a session argument (static URIs), so the
    snapshot only makes sense when exactly one session is active.
    """
    sessions = await session_manager.list_sessions()
    if not sessions:
        raise ResourceError(
            "no active PPSSPP session — start one with "
            "ppsspp_session(action=start, iso_path=...)"
        )
    if len(sessions) > 1:
        raise ResourceError(
            f"{len(sessions)} active sessions — snapshots require exactly "
            "one; use ppsspp_session_list and the session-argument tools "
            "(ppsspp_query / ppsspp_read_memory) instead"
        )
    return sessions[0].session_id


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
        async with session_client_with_transport(session_id) as (client, _t):
            status = await client.game_status()
    except Exception as e:
        raise to_tool_error(e) from e
    sess = await session_manager.get_session_state(session_id)
    return {"session_id": session_id,
            "restored": bool(sess.extra.get("restored")),
            "game": status}


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
        async with session_client_with_transport(session_id) as (client, _t):
            regs = await client.get_all_regs()
    except Exception as e:
        raise to_tool_error(e) from e
    sess = await session_manager.get_session_state(session_id)
    return {"session_id": session_id,
            "restored": bool(sess.extra.get("restored")),
            "registers": regs}
