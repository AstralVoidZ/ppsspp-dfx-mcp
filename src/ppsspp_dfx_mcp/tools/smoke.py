"""Smoke test tool wrapper.

1 tool exposed:
- ppsspp_smoke_test(session_id, checks?) — run a battery of health checks
  against an active PPSSPP session (ISO loaded / CPU running / WS connected
  / game_mode valid).

Async: uses session_client → PpssppDebugClient under the hood.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import Field

from ppsspp_dfx_mcp.errors import ArgsInvalid, to_tool_error
from ppsspp_dfx_mcp.models.smoke import CheckResult
from ppsspp_dfx_mcp.session.client_helper import (
    read_game_mode_addr,
    session_client,
)
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.smoke import SmokeTestResponse

SmokeTestOutput = derive_output_contract("SmokeTestOutput", SmokeTestResponse)

logger = logging.getLogger(__name__)

__all__ = ["smoke_test"]

_DEFAULT_CHECKS: tuple[str, ...] = (
    "iso_loaded",
    "cpu_running",
    "ws_connected",
    "game_mode_valid",
)


# Former docstring (kept as comment; description is now the TDQS docstring):
# Run a smoke test battery against an active PPSSPP session.
#
# Checks:
# iso_loaded       — game.status response has a non-empty game title.
# cpu_running      — game.status response reports paused=False.
# ws_connected     — WebSocket connect + version handshake succeeds.
# game_mode_valid  — read_u32(game_mode_addr) returns a non-zero value.
#
# Raises:
# ToolError: on session lookup failure or WS connect failure.
async def run_smoke_checks(
    session_id: str,
    checks: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Internal four-point session battery (v0.1.7 D5: absorbed into
    ppsspp_health(session_id=...); no longer an MCP tool). Returns
    (checks_as_dicts, overall_status)."""


async def smoke_test(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    checks: Annotated[
        list[str] | None,
        Field(
            default=None,
            description=(
                "Subset of checks to run (default: all). "
                "Valid values: 'iso_loaded', 'cpu_running', "
                "'ws_connected', 'game_mode_valid'."
            ),
        ),
    ] = None,
) -> SmokeTestOutput:
    """PURPOSE: Four-point session health check — iso_loaded, cpu_running, ws_connected, game_mode_valid.

    USAGE: session_id. NOT an ISO boot-acceptance test — use session wait_ready + analyze_log for boot triage.

    BEHAVIOR: READ-ONLY. Battery of probes.

    RETURNS: {checks[{name, passed, detail}], overall_status}."""
    selected = tuple(checks) if checks else _DEFAULT_CHECKS
    invalid = [c for c in selected if c not in _DEFAULT_CHECKS]
    if invalid:
        raise ArgsInvalid(f"invalid check(s) {invalid}; expected one of {_DEFAULT_CHECKS}")

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_health.session_battery",
            "session_id": session_id,
            "checks": selected,
        },
    )

    results: list[CheckResult] = []
    game_status: dict[str, Any] = {}

    try:
        async with session_client(session_id) as client:
            # ws_connected check passes by virtue of context entry success.
            for check in selected:
                if check == "ws_connected":
                    results.append(CheckResult(name=check, passed=True, detail="WS handshake OK"))
                    continue
                if check in ("iso_loaded", "cpu_running"):
                    if not game_status:
                        try:
                            game_status = await client.game_status()
                        except Exception as e:
                            results.append(CheckResult(name=check, passed=False, detail=str(e)))
                            continue
                    if check == "iso_loaded":
                        title = game_status.get("game") or game_status.get("title") or ""
                        results.append(
                            CheckResult(
                                name=check,
                                passed=bool(title),
                                detail=f"game={title!r}",
                            )
                        )
                    else:  # cpu_running
                        paused = game_status.get("paused", True)
                        results.append(
                            CheckResult(
                                name=check,
                                passed=not paused,
                                detail=f"paused={paused}",
                            )
                        )
                elif check == "game_mode_valid":
                    addr = read_game_mode_addr()
                    if addr == 0:
                        results.append(
                            CheckResult(
                                name=check,
                                passed=False,
                                detail="game_mode_addr not configured",
                            )
                        )
                        continue
                    try:
                        val = await client.read_u32(addr)
                        results.append(
                            CheckResult(
                                name=check,
                                passed=val != 0,
                                detail=f"game_mode=0x{val:08X}",
                            )
                        )
                    except Exception as e:
                        results.append(CheckResult(name=check, passed=False, detail=str(e)))
    except Exception as e:
        raise to_tool_error(e) from e

    overall = "pass" if all(r.passed for r in results) else "fail"
    checks_out = [{"name": r.name, "passed": r.passed, "detail": r.detail} for r in results]
    return checks_out, overall
