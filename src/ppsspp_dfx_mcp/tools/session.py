"""Session tool wrappers.

Thin wrappers around session.session_manager. Translates business
exceptions to ToolError. No business logic here.

2 tools exposed:
- ppsspp_session(action=start/stop/get/wait_ready, ...) — aggregate
  session lifecycle; 'wait_ready' (H0, 2026-09-07) polls a CPU-start
  probe read until the emulated CPU is up (see BootTimeout)
- ppsspp_session_list() — list active sessions (with idle GC side effect)
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.config import test_mode
from ppsspp_dfx_mcp.errors import ArgsInvalid, to_tool_error
from ppsspp_dfx_mcp.models.session import WaitReadyResult
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session import session_manager
from ppsspp_dfx_mcp.session.client_helper import validate_session_alive
from ppsspp_dfx_mcp.session.safe_boot import probe_cpu_ready
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract, flatten_union
from ppsspp_dfx_mcp.views.session import (
    SessionListResponse,
    SessionResponse,
    WaitReadyResponse,
)

# 🔴-1 联合契约：SDK 的 convert_result 会把契约未声明的键从
# structuredContent 中剥掉（partial=True 只是"校验通过"，键仍会丢）。
# 因此把每个分支的形状（各自 partial 派生 = 全可选）合并成一个 union
# TypedDict，键集为三者的并集。
_SessionResponseOut = derive_output_contract("SessionResponseOut", SessionResponse, partial=True)
_WaitReadyOut = derive_output_contract("WaitReadyOut", WaitReadyResponse, partial=True)
_SessionListOut = derive_output_contract("SessionListOut", SessionListResponse, partial=True)


SessionOutput = flatten_union("SessionOutput", _SessionResponseOut, _WaitReadyOut, _SessionListOut)

logger = logging.getLogger(__name__)

__all__ = ["session"]


_VALID_ACTIONS = ("list", "start", "stop", "get", "wait_ready")

# H0: probe address polled by wait_ready. 0x08804000 is the project's
# top.prx load base (addresses.yaml top_base) — the same probe the test
# harness and integration conftest have validated on real boots.
_DEFAULT_PROBE_ADDR = "0x08804000"
# Poll cadence: the probe is one cheap ticketed read; 1s keeps boot
# detection latency negligible against a 60-75s ISO load.
_WAIT_READY_POLL_INTERVAL_S = 1.0


async def _wait_ready_cpu(session_id: str, timeout_s: float, probe_addr: int) -> WaitReadyResult:
    """Poll a probe read until the emulated CPU is up (H0 helper).

    Delegates to the SINGLE shared probe implementation
    (session/safe_boot.probe_cpu_ready — sunk out of this tool so
    SessionManager's resilient start and wait_ready can never drift).
    Lock contract: NOT holding — same shape as
    wait_frames; the probe goes through the raw session-level
    transport, so health/list tools stay responsive (and early reads
    get clean CPU-not-started errors instead of SESSION_BUSY) while the
    boot wait runs.

    Raises:
        BootTimeout: probe never succeeded within timeout_s (wedge
            guidance embedded in the message; also raised after
            resilient-start exhaustion by SessionManager).
        SessionNotFound / SessionExpired: session gone or died mid-wait.
    """
    await validate_session_alive(session_id)
    # SessionNotFound here means the transport was never established —
    # propagate it as-is.
    transport = await session_manager.get_transport(session_id)
    ready = await probe_cpu_ready(
        transport,
        probe_addr=probe_addr,
        budget_s=timeout_s,
        poll_interval_s=_WAIT_READY_POLL_INTERVAL_S,
    )
    return WaitReadyResult(
        ready=True,
        elapsed_s=ready["elapsed_s"],
        probe_addr=probe_addr,
        probe_value=ready["probe_value"],
    )


# Former docstring (kept as comment; description is now the TDQS docstring):
# Aggregate tool for PPSSPP session lifecycle.
#
# Action mapping:
# action=start → start_session(iso_path)  [async]
# action=stop  → stop_session(session_id)
# action=get   → get_session_state(session_id)
#
# Raises:
# ToolError: on invalid action, missing required param, or business error.
@mcp.tool(
    name="ppsspp_session",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    ),
)
@translate_tool_errors
async def session(
    action: Annotated[
        Literal["list", "start", "stop", "get", "wait_ready"],
        Field(
            description=(
                "Session operation. Valid values:\n"
                "- 'list': list all active sessions (no other params). "
                "Idle sessions (>30 min) are auto-GC'd as a side effect; "
                "returns {sessions, count}. NOT a per-session health "
                "probe — use ppsspp_health(session_id=…) for that.\n"
                "- 'start': launch a new PPSSPP session (requires iso_path). "
                "Set wait_ready=true to block until the emulated CPU is up "
                "(same probe/budget semantics as 'wait_ready').\n"
                "- 'stop': terminate an existing session (requires session_id).\n"
                "- 'get': query session health (requires session_id).\n"
                "- 'wait_ready': block until the emulated CPU has started "
                "(requires session_id). Call this after 'start' BEFORE any "
                "memory/disassembly tool — PPSSPP answers WebSocket before "
                "the ISO finishes booting, and early reads fail with "
                "'CPU not started'."
            ),
        ),
    ],
    iso_path: str | None = Field(
        default=None,
        description="Absolute path to the ISO file (required when action=start).",
    ),
    session_id: str | None = Field(
        default=None,
        description="Session ID (required when action=stop / get / wait_ready).",
    ),
    timeout_s: Annotated[
        float,
        Field(
            default=75.0,
            description=(
                "Boot budget in seconds (action=wait_ready, "
                "action=start with wait_ready=true, or the per-attempt "
                "CPU-ready budget when action=start with resilient=true; "
                "default 75, clamped to [1, 300])."
            ),
        ),
    ] = 75.0,
    probe_addr: Annotated[
        str,
        Field(
            default=_DEFAULT_PROBE_ADDR,
            description=(
                "Hex address polled by the readiness probe "
                "(action=wait_ready, action=start with wait_ready=true, "
                "or the resilient-start gate; default '0x08804000', the "
                "project's top.prx load base)."
            ),
        ),
    ] = _DEFAULT_PROBE_ADDR,
    wait_ready: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "action=start only: block until the emulated CPU is "
                "ready before returning (same probe/budget as "
                "action=wait_ready; raises [BOOT_TIMEOUT] on wedge "
                "suspicion). Fake test mode is ready immediately. "
                "Default false keeps the historical two-call flow."
            ),
        ),
    ] = False,
    resilient: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "action=start only: self-healing boot — on wedge "
                "evidence (CPU-ready probe exhausted, handshake never "
                "accepted, process died) the launcher is torn down, the "
                "GPU-backend failure blacklist is quarantined (rename), "
                "and the session relaunches with the SAME session_id up "
                "to 2 retries; the response carries recovered=N (0 = "
                "first launch). Exhaustion raises [BOOT_TIMEOUT]. "
                "Ignored in fake mode."
            ),
        ),
    ] = False,
) -> SessionOutput:
    """PURPOSE: Start / stop / inspect PPSSPP debug sessions — action=list / start / stop / get / wait_ready; wait_ready blocks until the emulated CPU is up.

    USAGE: action='list' takes no other params and returns {sessions, count} (idle sessions >30min are auto-GC'd as a side effect; NOT a per-session health probe — use ppsspp_health(session_id=...) for that); action='start' needs iso_path (pass wait_ready=true to block until the CPU is up in the same call); stop/get/wait_ready need session_id. Call wait_ready AFTER start and BEFORE any memory tool — PPSSPP answers WebSocket before the CPU boots. start(resilient=true) self-heals boot wedges (blacklist quarantine + relaunch with the same session_id, ≤2 retries).

    BEHAVIOR: STATE-CHANGE. start spawns a PPSSPP subprocess + WS debugger; stop terminates it (never taskkill the process yourself); wait_ready polls the probe lock-free and fails [BOOT_TIMEOUT] on wedge suspicion; list/get are read-only.

    RETURNS: action=start/get → SessionResponse {session_id, iso_path, pid, ws_url, created_at, last_active_at, exec_count, ws_connected, recovered, restored (1 = session record was restored from sessions.json after a server restart, 0 = created in this process), ppsspp_version}; action=wait_ready → {action, ready, elapsed_s, probe_addr, probe_value, note} — elapsed_s is THIS call's wait duration, not the start→ready total; action=list → {sessions: [SessionResponse...], count}."""
    if action not in _VALID_ACTIONS:
        raise ArgsInvalid(f"invalid action={action!r}; expected one of {_VALID_ACTIONS}")

    logger.info("tool_call", extra={"tool": "ppsspp_session", "action": action})
    clamped_timeout = min(max(float(timeout_s), 1.0), 300.0)
    try:
        if action == "list":
            sessions = await session_manager.list_sessions()
            return SessionListResponse.from_sessions(sessions).model_dump(mode="json")
        if action == "start":
            if not iso_path:
                raise ArgsInvalid("iso_path is required when action=start")
            # S10 (review v2): the start branch used to be the one path
            # that skipped the zero check — resilient mode's readiness
            # gate would then probe address 0 (the NUL page) instead of
            # a real CPU-liveness signal.
            probe_int_start = parse_address(probe_addr)
            if probe_int_start == 0:
                raise ArgsInvalid(f"probe_addr must be a valid hex address, got {probe_addr!r}")
            sess = await session_manager.start_session(
                iso_path,
                resilient=resilient,
                ready_timeout_s=clamped_timeout,
                probe_addr=probe_int_start,
            )
            if wait_ready and test_mode() != "fake":
                # G5: one-call boot — same probe/budget semantics as
                # action=wait_ready, inlined after a successful start.
                probe_int = parse_address(probe_addr)
                if probe_int == 0:
                    raise ArgsInvalid(f"probe_addr must be a valid hex address, got {probe_addr!r}")
                await _wait_ready_cpu(sess.session_id, clamped_timeout, probe_int)
        elif action == "stop":
            if not session_id:
                raise ArgsInvalid("session_id is required when action=stop")
            sess = await session_manager.stop_session(session_id)
        elif action == "wait_ready":
            if not session_id:
                raise ArgsInvalid("session_id is required when action=wait_ready")
            probe_int = parse_address(probe_addr)
            if probe_int == 0:
                raise ArgsInvalid(f"probe_addr must be a valid hex address, got {probe_addr!r}")
            if test_mode() == "fake":
                result = WaitReadyResult(
                    ready=True,
                    elapsed_s=0.0,
                    probe_addr=probe_int,
                    probe_value=None,
                    note="fake test mode has no boot concept — ready immediately",
                )
            else:
                result = await _wait_ready_cpu(session_id, clamped_timeout, probe_int)
            return WaitReadyResponse.from_result(result).model_dump(mode="json")
        else:  # action == "get"
            if not session_id:
                raise ArgsInvalid("session_id is required when action=get")
            sess = await session_manager.get_session_state(session_id)
    except Exception as e:
        raise to_tool_error(e) from e
    return SessionResponse.from_session(sess).model_dump(mode="json")
