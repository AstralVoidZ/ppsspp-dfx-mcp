"""Replay tool wrapper.

1 tool exposed:
- ppsspp_replay(action, ...) — aggregate PPSSPP Replay protocol proxy

Actions (P0+P1, 10 total):
- 'begin'        → start/resume recording
- 'abort'        → abort any recording or execution
- 'flush'        → flush recorded data (returns version + base64)
- 'execute'      → execute a recorded replay (requires version + base64)
- 'status'       → query {executing, saving}
- 'time_get'     → get base RTC
- 'time_set'     → set base RTC (requires value)
- 'save'         → flush + time_get + write .ppr file (requires file_path;
#                    NOTE: both 'flush' and 'save' CONSUME the recording
#                    buffer — after a flush, a following save fails with
#                    REPLAY_EMPTY; record again to save;
                   bare file name, always under .ppsspp-dfx/output/replays/)
- 'load'         → read .ppr + execute (requires file_path; same containment)
- 'wait_complete'→ poll replay.status until executing=False

Spike evidence (docs/experiment/experiment_ppsspp_replay_spike_v1.md):
- U1: input.buttons.press recorded accurately during replay.begin
- U2: gpu.buffer.screenshot unusable during recording (requires stepping);
  use state_probe (read_u32) as lightweight observation instead
- U3: replay.execute does not auto-end — wait_complete added to poll

Async: uses session_client → PpssppDebugClient. Tools call DebugClient
domain methods (replay_begin / replay_abort / ...) directly.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError
from ppsspp_dfx_mcp.models.replay import (
    PPRFile,
    ReplayResult,
    parse_replay_blob_b64,
)
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.session.client_helper import resolve_session_id, session_client
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import (
    require_int_not_bool,
    resolve_output_path,
    translate_tool_errors,
)
from ppsspp_dfx_mcp.views.replay import ReplayResponse

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    ReplayOutput = dict[str, Any]
else:
    ReplayOutput = derive_output_contract("ReplayOutput", ReplayResponse)

logger = logging.getLogger(__name__)

__all__ = ["replay"]

_REPLAY_ACTIONS: tuple[str, ...] = (
    "begin",
    "abort",
    "flush",
    "execute",
    "status",
    "time_get",
    "time_set",
    "save",
    "load",
    "wait_complete",
)

# The boot-aligned
# replay sequence. PPSSPP replays by ABSOLUTE CoreTiming timestamps whose
# epoch is the recording session's boot; aligning a fresh boot to them is
# the only usable cross-session playback mode.
_BOOT_ALIGNED_SEQUENCE = (
    "boot-aligned replay: (1) ppsspp_replay(execute/load) — this call; "
    "(2) ppsspp_step(action='reset') — zeroes game-t to the recording "
    "epoch; (3) ppsspp_session(action='wait_ready'); (4) wait until "
    "boot + estimated_end_s of game time (input injects from boot+t0_s); "
    "(5) ppsspp_replay(action='abort') — executing never clears on its "
    "own; abort restores real input and is the ONLY completion signal."
)


def _compute_base64_size(b64: str) -> int:
    """Decode base64 string and return its binary byte length.

    Returns 0 if b64 is empty or invalid. Centralized here so flush /
    save / load share the same error-tolerant sizing logic — previously
    each action duplicated the try/except, with load missing it entirely
    (a corrupted .ppr file would raise unhandled binascii.Error).
    """
    if not b64:
        return 0
    try:
        return len(base64.b64decode(b64, validate=True))
    except (ValueError, binascii.Error):
        return 0


async def _ensure_replay_idle(client: Any) -> bool:
    """Async guard; returns True when an abort was issued."""
    try:
        status = await client.replay_status()
    except Exception as e:
        logger.warning("replay_status probe failed before execute: %s", e)
        return False
    if bool(status.get("executing")) or bool(status.get("saving")):
        await client.replay_abort()
        return True
    return False


def _span_fields(b64: str) -> dict[str, Any]:
    """t0/estimated_end from the blob; {} when unparseable (never
    fatal — the estimate is advisory)."""
    try:
        span = parse_replay_blob_b64(b64)
    except ValueError as e:
        logger.warning("replay blob span unavailable: %s", e)
        return {}
    return {
        "t0_s": round(span.t0_s, 2),
        "estimated_end_s": round(span.end_s, 2),
        "event_count": span.event_count,
    }


# LONG-TOOL: one tool aggregates record/execute/status/abort/time_get/
# time_set/save/load/wait_complete behind a single action enum — splitting
# it would multiply the tool surface the baseline and docs pin.
@mcp.tool(
    name="ppsspp_replay",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
@translate_tool_errors
async def replay(
    action: Annotated[
        Literal[
            "begin",
            "abort",
            "flush",
            "execute",
            "status",
            "time_get",
            "time_set",
            "save",
            "load",
            "wait_complete",
        ],
        Field(
            description=(
                "Replay operation. Valid values:\n"
                "- 'begin': begin/resume recording.\n"
                "- 'abort': abort any recording or execution.\n"
                "- 'flush': flush recorded data (returns version + base64).\n"
                "- 'execute': execute a replay (requires version + "
                "base64_input). ONLY loads the event table — follow the "
                "boot-aligned sequence in the response (reset + wait + "
                "abort) or input never injects.\n"
                "- 'status': query {executing, saving}.\n"
                "- 'time_get': get base RTC.\n"
                "- 'time_set': set base RTC (requires value). WARNING: "
                "rewinds the game-visible wall clock on the RUNNING "
                "session — pollutes every in-game timer.\n"
                "- 'save': flush + time_get + write .ppr file (requires "
                "file_path: bare file name under output/replays/).\n"
                "- 'load': read .ppr + execute (requires file_path; same "
                "containment). Returns t0_s / estimated_end_s and the "
                "boot-aligned sequence.\n"
                "- 'wait_complete': poll replay.status until "
                "executing=False — NOTE: executing never clears on its "
                "own (only abort clears it), so this always times out on "
                "an un-aborted replay; kept for recording-completion "
                "checks and backwards compatibility."
            ),
        ),
    ],
    session_id: Annotated[
        str | None,
        Field(
            description=(
                "Active session ID; omit to auto-resolve when exactly one session is active."
            ),
        ),
    ] = None,
    version: Annotated[
        int,
        Field(
            default=0,
            description=(
                "Required for action='execute'. Replay format version (from a "
                "prior replay.flush). Not used by the other actions. The "
                "schema default of 0 exists for legacy callers -- do NOT rely "
                "on it when the action is 'execute'."
            ),
        ),
    ] = 0,
    base64_input: Annotated[
        str,
        Field(
            default="",
            description=(
                "Base64-encoded replay data (from a prior replay.flush). "
                "Required for action='execute'; ignored for all other actions."
            ),
        ),
    ] = "",
    value: Annotated[
        int,
        Field(
            default=0,
            description=(
                "Required for action='time_set'. Base RTC value in seconds "
                "(uint32). Not used by the other actions. The schema default "
                "of 0 exists for legacy callers -- do NOT rely on it when the "
                "action is 'time_set'."
            ),
        ),
    ] = 0,
    timeout_ms: Annotated[
        int,
        Field(
            default=10000,
            ge=100,
            le=25000,
            description=(
                "Total timeout in milliseconds for action='wait_complete' "
                "(default 10000 = 10s, clamped 100..25000). The ceiling is "
                "25s because the MCP client aborts a tool call at ~30s: a "
                "larger value could never be honoured. Ignored for all other "
                "actions."
            ),
        ),
    ] = 10000,
    interval_ms: Annotated[
        int,
        Field(
            default=100,
            ge=10,
            le=5000,
            description=(
                "Polling interval in milliseconds for action='wait_complete' "
                "(default 100ms, clamped 10..5000 — below 10 the poll "
                "degenerates to a busy loop on the WS). Ignored for all "
                "other actions."
            ),
        ),
    ] = 100,
    file_path: Annotated[
        str,
        Field(
            default="",
            description=(
                "Bare .ppr file NAME (no directory parts) for action='save' "
                "/ action='load'. The file is always placed under the "
                "server-managed directory .ppsspp-dfx/output/replays/ — "
                "absolute paths and path separators are rejected. Required "
                "for save / load; ignored for all other actions."
            ),
        ),
    ] = "",
    session_note: Annotated[
        str,
        Field(
            default="",
            description=(
                "Optional human-readable note embedded in the .ppr file "
                "when action='save'. Ignored for all other actions."
            ),
        ),
    ] = "",
    restore_rtc: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Whether to restore base_rtc via replay.time_set before "
                "execute when action='load' (default False). true "
                "sets the game-visible wall clock back to the recording "
                "moment — pollutes EVERY timer of the running session "
                "(attract timeouts, clocks, cooldowns) because game time "
                "= rtcBaseTime + elapsed. Only use for deterministic "
                "replays, and prefer setting it BEFORE the reset of the "
                "boot-aligned sequence so the game boots on the shifted "
                "base. Ignored for all other actions."
            ),
        ),
    ] = False,
) -> ReplayOutput:
    """PURPOSE: Aggregate PPSSPP replay subsystem — record input sequences, execute them, and save/load .ppr recordings.

    USAGE: session_id optional when exactly one session is active; actions: begin/abort/flush/execute/status/time_get/time_set/save/load/wait_complete; execute needs version + base64_input; time_set needs value; save/load take a bare file name (always under output/replays/).

    BEHAVIOR: STATE-CHANGE. Recording requires the CPU RUNNING (real input timing); screenshots are rejected while recording. Replay timelines use ABSOLUTE game-clock timestamps anchored at the RECORDING session's boot — a replay only injects correctly when a fresh boot's clock is aligned to them: execute/load ONLY loads the event table and returns t0_s / estimated_end_s + the boot-aligned sequence (reset -> wait_ready -> wait boot+estimated_end_s -> abort); it does NOT play by itself. executing/saving NEVER clear on their own — only abort clears them — so wait_complete times out on any un-aborted replay; completion = the timeline estimate + explicit abort. execute/load auto-abort a live executing/saving state first. restore_rtc defaults to False: setting it rewinds the game-visible wall clock of the RUNNING session and pollutes every in-game timer; when needed, set it before the boot-aligned reset.

    RETURNS: {action, executing, saving, version, size, base64, base_rtc, data} — execute/load data carries t0_s, estimated_end_s, event_count and boot_aligned_sequence; fields depend on the action."""
    session_id = await resolve_session_id(session_id)
    if action not in _REPLAY_ACTIONS:
        raise ArgsInvalid(f"invalid action={action!r}; expected one of {_REPLAY_ACTIONS}")
    if action == "execute":
        version = require_int_not_bool(version, "version")
        if version == 0:
            # A-4 (review v4): require_int_not_bool also closes the
            # `version=True` bypass (bool is an int; True == 1).
            raise ArgsInvalid(
                "version is required when action=execute "
                "(version=0 is not a valid replay version — obtain it "
                "from a prior replay.flush response)"
            )
        if not base64_input:
            raise ArgsInvalid("base64_input is required when action=execute")
        # G-8: reject undecodable base64 BEFORE any session I/O. Without this,
        # a malformed blob is passed to PPSSPP and the call silently
        # "succeeds" (the event table loads empty), so a caller can't tell a
        # bad payload from a replay with no events. validate=True makes the
        # decoder reject non-alphabet characters instead of discarding them.
        try:
            base64.b64decode(base64_input, validate=True)
        except (ValueError, binascii.Error) as e:
            raise ArgsInvalid(f"base64_input is not valid base64 (action=execute): {e}") from e
    ppr_path = None
    if action in ("save", "load"):
        if not file_path:
            raise ArgsInvalid(f"file_path is required when action={action}")
        # Resolve + contain the path BEFORE any session I/O so an
        # illegal path fails fast without contacting PPSSPP.
        ppr_path = resolve_output_path("replays", file_path)

    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_replay", "action": action, "session_id": session_id},
    )

    async with session_client(session_id) as client:
        if action == "begin":
            resp = await client.replay_begin()
            result = ReplayResult(action=action, data=resp)
        elif action == "abort":
            resp = await client.replay_abort()
            result = ReplayResult(action=action, data=resp)
        elif action == "flush":
            resp = await client.replay_flush()
            version_val = int(resp.get("version", 0))
            b64 = str(resp.get("base64", ""))
            size = _compute_base64_size(b64)
            result = ReplayResult(
                action=action,
                version=version_val,
                size=size,
                base64=b64,
                data=resp,
            )
        elif action == "execute":
            await _ensure_replay_idle(client)
            resp = await client.replay_execute(version=version, base64=base64_input)
            result = ReplayResult(
                action=action,
                version=version,
                base64=base64_input,
                data={
                    **_span_fields(base64_input),
                    "boot_aligned_sequence": _BOOT_ALIGNED_SEQUENCE,
                    **resp,
                },
            )
        elif action == "status":
            resp = await client.replay_status()
            result = ReplayResult(
                action=action,
                executing=bool(resp.get("executing", False)),
                saving=bool(resp.get("saving", False)),
                data=resp,
            )
        elif action == "time_get":
            resp = await client.replay_time_get()
            result = ReplayResult(
                action=action,
                base_rtc=int(resp.get("value", 0)),
                data=resp,
            )
        elif action == "time_set":
            rtc = require_int_not_bool(value, "value")
            if not 0 <= rtc <= 0xFFFFFFFF:
                # A-4 (review v4): the field advertises "uint32"; out-of-range
                # values used to reach PPSSPP with undefined behavior while
                # the response echoed the original value, masking the wrap.
                raise ArgsInvalid(
                    f"value must be a uint32 (0..4294967295) — base RTC seconds, got {value!r}"
                )
            resp = await client.replay_time_set(value=rtc)
            result = ReplayResult(
                action=action,
                base_rtc=rtc,
                data=resp,
            )
        elif action == "save":
            # file_path 已在会话建立前解析（早于任何 WS I/O）；此断言仅
            # 向类型检查器传达该不变式，不是运行时校验。
            assert ppr_path is not None
            # 1. flush recorded data
            flush_resp = await client.replay_flush()
            version_val = int(flush_resp.get("version", 0))
            b64 = str(flush_resp.get("base64", ""))
            size = _compute_base64_size(b64)
            if size <= 0:
                # Measured 2026-09-08: an empty capture used to be
                # written as a 0-byte .ppr with an ok response; the
                # corruption only surfaced at load time as a vague
                # protocol error. Fail HERE instead — note the flush
                # already consumed the (empty) recording.
                raise ToolError(
                    "no frames captured — the replay recorder was "
                    "empty at save time (record: begin → advance a "
                    "few gameplay frames → save). The flush already "
                    "consumed the empty recording and NO .ppr file "
                    "was written.",
                    code="REPLAY_EMPTY",
                )
            # 2. get base_rtc
            time_resp = await client.replay_time_get()
            base_rtc_val = int(time_resp.get("value", 0))
            # 3. write .ppr file
            ppr = PPRFile(
                version=version_val,
                base64=b64,
                base_rtc=base_rtc_val,
                recorded_at=time.time(),
                session_note=session_note,
            )
            # Server-controlled directory — the
            # caller may only influence the file name (prompt-injected
            # absolute paths used to allow arbitrary file overwrite).
            ppr_path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(ppr.to_dict(), indent=2)
            try:
                await asyncio.to_thread(ppr_path.write_text, payload, encoding="utf-8")
            except OSError as e:
                # replay_flush already consumed and reset the recorder,
                # so a failed write must not lose the recording. The
                # payload used to be embedded in the
                # ToolError text — a long recording made the error
                # itself megabytes on the MCP text channel. Rescue it
                # to a server-generated file; the error names the path.
                rescue_name = f"replay_rescue_{time.strftime('%Y%m%d_%H%M%S')}.ppr"
                rescue_path = resolve_output_path("replays", rescue_name)
                rescued = False
                try:
                    rescue_path.parent.mkdir(parents=True, exist_ok=True)
                    rescue_doc = json.dumps(
                        PPRFile(
                            version=version_val,
                            base64=b64,
                            base_rtc=base_rtc_val,
                            recorded_at=time.time(),
                            session_note=session_note,
                        ).to_dict(),
                        indent=2,
                    )
                    await asyncio.to_thread(rescue_path.write_text, rescue_doc, encoding="utf-8")
                    rescued = True
                except Exception as rescue_err:  # noqa: BLE001
                    logger.error("replay rescue write failed too: %s", rescue_err)
                logger.error("replay save: writing %s failed: %s", ppr_path, e)
                where = (
                    f"the rescue file {rescue_name!r} — replay(action='load', "
                    f"file_path={rescue_name!r}) restores it"
                    if rescued
                    else "but could NOT be rescued to disk"
                )
                raise ToolError(
                    f"failed to write .ppr file {file_path}: "
                    f"{type(e).__name__}. The recording was already flushed "
                    f"from PPSSPP and is preserved at {where}. "
                    f"base64 payload size={size}.",
                    code="INTERNAL",
                ) from e
            result = ReplayResult(
                action=action,
                version=version_val,
                size=size,
                base64=b64,
                base_rtc=base_rtc_val,
                data={
                    "file_path": str(ppr_path),
                    "ppr_format_version": ppr.ppr_format_version,
                },
            )
        elif action == "load":
            # 1. read .ppr file (contained to output/replays/;
            # already resolved + validated before the session opened).
            # ppr_path 由上面的 action in ("save","load") 分支保证非空。
            assert ppr_path is not None
            if not ppr_path.is_file():
                raise ArgsInvalid(f".ppr file not found: {file_path}")
            try:
                raw = json.loads(await asyncio.to_thread(ppr_path.read_text, encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise ArgsInvalid(f"invalid .ppr file (JSON parse error): {e}") from e
            try:
                ppr = PPRFile.from_dict(raw)
            except (ValueError, TypeError) as e:
                raise ArgsInvalid(f"invalid .ppr file: {e}") from e
            # A live executing/saving state must not mix with the
            # new event table.
            await _ensure_replay_idle(client)
            # 2. restore base_rtc (optional; default False, see
            # the restore_rtc description for the pollution warning).
            if restore_rtc:
                await client.replay_time_set(value=ppr.base_rtc)
            # 3. execute
            exec_resp = await client.replay_execute(version=ppr.version, base64=ppr.base64)
            result = ReplayResult(
                action=action,
                version=ppr.version,
                size=_compute_base64_size(ppr.base64),
                base64=ppr.base64,
                base_rtc=ppr.base_rtc,
                executing=True,
                data={
                    **_span_fields(ppr.base64),
                    "boot_aligned_sequence": _BOOT_ALIGNED_SEQUENCE,
                    "file_path": str(ppr_path),
                    "restore_rtc_applied": restore_rtc,
                    **exec_resp,
                },
            )
        else:  # wait_complete
            resp = await client.replay_wait_complete(timeout_ms=timeout_ms, interval_ms=interval_ms)
            iterations = int(resp.get("_wait_iterations", 0))
            # Build a clean data dict without the internal _wait_iterations
            # key (already surfaced via the dedicated wait_iterations
            # field above). Avoids mutating the transport's response dict.
            clean_data = {k: v for k, v in resp.items() if k != "_wait_iterations"}
            result = ReplayResult(
                action=action,
                executing=bool(resp.get("executing", False)),
                saving=bool(resp.get("saving", False)),
                wait_iterations=iterations,
                data=clean_data,
            )
    return ReplayResponse.from_result(result).model_dump(mode="json")
