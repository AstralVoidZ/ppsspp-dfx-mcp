"""Smoke test — internal four-point session battery (absorbed into health).

v0.1.7 D5: ppsspp_smoke_test was absorbed into ppsspp_health(session_id=...).
The implementation lives in run_smoke_checks() which health delegates to.
No MCP tool is registered from this module.
"""

from __future__ import annotations

import logging
from typing import Any

from ppsspp_dfx_mcp.errors import to_tool_error
from ppsspp_dfx_mcp.session.client_helper import (
    read_game_mode_addr,
    session_client,
)

logger = logging.getLogger(__name__)

_DEFAULT_CHECKS: tuple[str, ...] = (
    "iso_loaded",
    "cpu_running",
    "ws_connected",
    "game_mode_valid",
)


async def run_smoke_checks(
    session_id: str,
    checks: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Run the four-point session battery and return (checks, overall).

    Checks: iso_loaded / cpu_running / ws_connected / game_mode_valid.
    Always returns a tuple — never None — even on errors (degraded facts).
    """
    selected = tuple(checks) if checks else _DEFAULT_CHECKS
    invalid = [c for c in selected if c not in _DEFAULT_CHECKS]
    if invalid:
        raise to_tool_error(
            ValueError(f"invalid check(s) {invalid}; expected one of {_DEFAULT_CHECKS}")
        )

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_health.session_battery",
            "session_id": session_id,
            "checks": selected,
        },
    )

    results: list[dict[str, Any]] = []
    game_status: dict[str, Any] = {}

    try:
        async with session_client(session_id) as client:
            for check in selected:
                if check == "ws_connected":
                    results.append({"name": check, "passed": True, "detail": "WS handshake OK"})
                    continue
                if check in ("iso_loaded", "cpu_running"):
                    if not game_status:
                        try:
                            game_status = await client.game_status()
                        except Exception as e:
                            results.append({"name": check, "passed": False, "detail": str(e)})
                            continue
                    if check == "iso_loaded":
                        title = game_status.get("game") or game_status.get("title") or ""
                        results.append(
                            {"name": check, "passed": bool(title), "detail": f"game={title!r}"}
                        )
                    else:
                        paused = game_status.get("paused", True)
                        results.append(
                            {"name": check, "passed": not paused, "detail": f"paused={paused}"}
                        )
                elif check == "game_mode_valid":
                    addr = read_game_mode_addr()
                    if addr == 0:
                        results.append(
                            {
                                "name": check,
                                "passed": False,
                                "detail": "game_mode_addr not configured",
                            }
                        )
                        continue
                    try:
                        val = await client.read_u32(addr)
                        results.append(
                            {"name": check, "passed": val != 0, "detail": f"game_mode=0x{val:08X}"}
                        )
                    except Exception as e:
                        results.append({"name": check, "passed": False, "detail": str(e)})
    except Exception as e:
        results.append({"name": "session_error", "passed": False, "detail": str(e)})

    overall = "pass" if all(r["passed"] for r in results) and results else "fail"
    return results, overall
