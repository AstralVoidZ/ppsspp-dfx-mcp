"""Smoke test — internal four-point session battery (absorbed into health).

ppsspp_smoke_test was absorbed into ppsspp_health(session_id=...).
The implementation lives in run_smoke_checks() which health delegates to.
No MCP tool is registered from this module.
"""

from __future__ import annotations

import logging
from typing import Any

from ppsspp_dfx_mcp.core.value_staleness import classify_probe_reading
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

# How a probe's value came to be.
#
# `passed` alone cannot carry this. A probe that read 0 and a probe whose
# read raised both end up passed=false, yet they say opposite things about
# the world: the first is a fact about the game, the second a fact about
# the tooling, and they call for different next actions. Every result
# therefore states which of these produced it, and a failed read carries no
# `value` key at all -- reporting 0 for "could not read" is exactly the
# silent failure this feature exists to remove.
VALUE_OK = "ok"
VALUE_FAILED = "failed"
VALUE_NOT_CONFIGURED = "not_configured"


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
                    # Reaching here means session_client yielded, i.e. the
                    # transport is up, so this value is observed not assumed.
                    results.append(
                        {
                            "name": check,
                            "passed": True,
                            "detail": "WS handshake OK",
                            "value_status": VALUE_OK,
                        }
                    )
                    continue
                if check in ("iso_loaded", "cpu_running"):
                    if not game_status:
                        try:
                            game_status = await client.game_status()
                        except Exception as e:
                            results.append(
                                {
                                    "name": check,
                                    "passed": False,
                                    "detail": str(e),
                                    "value_status": VALUE_FAILED,
                                }
                            )
                            continue
                    if check == "iso_loaded":
                        title = game_status.get("game") or game_status.get("title") or ""
                        results.append(
                            {
                                "name": check,
                                "passed": bool(title),
                                "detail": f"game={title!r}",
                                "value_status": VALUE_OK,
                            }
                        )
                    else:
                        # The old code defaulted a missing key to
                        # paused=True, so a malformed game.status read as
                        # "the CPU is paused". Absence is now reported as
                        # absence.
                        if "paused" not in game_status:
                            results.append(
                                {
                                    "name": check,
                                    "passed": False,
                                    "detail": (
                                        "game_status did not report 'paused'; "
                                        "CPU state is unknown, not paused"
                                    ),
                                    "value_status": VALUE_FAILED,
                                }
                            )
                            continue
                        paused = game_status["paused"]
                        results.append(
                            {
                                "name": check,
                                "passed": not paused,
                                "detail": f"paused={paused}",
                                "value_status": VALUE_OK,
                            }
                        )
                elif check == "game_mode_valid":
                    addr = read_game_mode_addr()
                    if addr == 0:
                        results.append(
                            {
                                "name": check,
                                "passed": False,
                                "detail": "game_mode_addr not configured",
                                "value_status": VALUE_NOT_CONFIGURED,
                            }
                        )
                        continue
                    try:
                        val = await client.read_u32(addr)
                    except Exception as e:
                        # No `value` key. Reporting 0 here would be a
                        # fabricated reading, indistinguishable from a game
                        # whose game_mode really is 0.
                        results.append(
                            {
                                "name": check,
                                "passed": False,
                                "detail": str(e),
                                "value_status": VALUE_FAILED,
                            }
                        )
                        continue
                    # A successful read of zero is NOT the
                    # same as a stale address. The classifier keeps a
                    # per-session consecutive-zero streak, so a drifted probe
                    # address surfaces as
                    # `stale_address_suspected` instead of passing silently
                    # with value=0 forever.
                    status, stale_note = classify_probe_reading(session_id, addr, val)
                    detail = f"game_mode=0x{val:08X}"
                    if stale_note:
                        detail = f"{detail}; {stale_note}"
                    results.append(
                        {
                            "name": check,
                            "passed": val != 0,
                            "detail": detail,
                            "value": val,
                            "value_status": status,
                        }
                    )
    except Exception as e:
        results.append(
            {
                "name": "session_error",
                "passed": False,
                "detail": str(e),
                "value_status": VALUE_FAILED,
            }
        )

    overall = "pass" if all(r["passed"] for r in results) and results else "fail"
    return results, overall
