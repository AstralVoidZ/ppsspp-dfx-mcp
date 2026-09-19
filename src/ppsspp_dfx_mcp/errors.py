"""ToolError + business exception translation.

Business exceptions → MCP ToolError.  Clients only see ToolError.
Business exception details go to stderr structured JSON logs.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable

from mcp.server.mcpserver.exceptions import ToolError as _SDKToolError

from ppsspp_dfx_mcp.core import proc


class ToolError(_SDKToolError):
    """Unified error type exposed to MCP clients.

    Inherits the SDK's ToolError so the MCPServer request handler
    classifies business errors as *anticipated*: the client receives
    ``is_error=True`` with our message in content text, and the server
    logs at INFO without a traceback.

    Raised by `to_tool_error()` when a business exception needs to be
    surfaced to the MCP client.  The original exception is preserved
    in `__cause__` for stderr logging.

    Subclasses define `code` as a class attribute. When raised directly
    (not via to_tool_error), `self.code` resolves to the class attribute.
    When wrapped by to_tool_error, the class attribute is preserved.
    """

    code: str = "INTERNAL"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code

    def __str__(self) -> str:
        # Render as "[CODE] message": the SDK has no structured error-data
        # channel on CallToolResult — it renders str(exc) into TextContent —
        # so this prefix is the only machine-readable error category agents
        # get. Single-point override: raise sites keep plain messages; the
        # code prefix is added only at render time.
        return f"[{self.code}] {super().__str__()}"


# ── Business exceptions ─────────────────────────────────────────────────


class ArgsInvalid(ToolError):
    """Tool arguments failed validation the input schema cannot express
    (range / cross-field / file-content constraints). The agent can fix
    these by adjusting its own call — never a server malfunction."""

    code = "ARGS_INVALID"


class ConfigInvalid(ToolError):
    """Configuration validation failed at startup."""

    code = "CONFIG_INVALID"


class PpssppError(ToolError):
    """Base class for PPSSPP business exceptions."""

    code = "PPSSPP_ERROR"


class PpssppNotFound(PpssppError):
    """PPSSPP executable not found at the configured path."""

    code = "PPSSPP_NOT_FOUND"


class IsoNotFound(PpssppError):
    """ISO file does not exist."""

    code = "ISO_NOT_FOUND"


class WsConnectFailed(PpssppError):
    """WebSocket connection to PPSSPP failed."""

    code = "WS_CONNECT_FAILED"


class SessionNotFound(ToolError):
    """Session ID not found in active sessions."""

    code = "SESSION_NOT_FOUND"


class SessionExpired(ToolError):
    """Session process is no longer alive."""

    code = "SESSION_EXPIRED"


class SessionBusy(ToolError):
    """Another tool call currently owns the session.

    Session-level tool calls are serialized by a per-session lock (the
    WsTransport, the GameStateObserver per-event queues, and the global
    CPU stepping state are all single-consumer). Instead of silently
    queueing behind a long-running call (e.g. a 5-minute wait_frames),
    a caller that waits longer than the busy timeout gets this explicit
    error and can retry.

    A1 (2026-09-10): when the lock is held by a detached background batch,
    the message names the batch id and points at ppsspp_batch_status /
    ppsspp_batch_cancel instead of a blind retry.
    """

    code = "SESSION_BUSY"


class SessionAlreadyExists(ToolError):
    """A session is already active for the requested resource."""

    code = "SESSION_ALREADY_EXISTS"


class SessionAmbiguous(ToolError):
    """G3 (best-practice gap audit): session_id omitted while 2+ sessions
    are active — auto-resolution is only safe for the single-session case.

    The message lists every active session_id so the caller can pass one
    explicitly (or stop the extras) without a second lookup call.
    """

    code = "SESSION_AMBIGUOUS"


class BootTimeout(ToolError):
    """The emulated CPU did not start within the boot budget.

    PPSSPP answers WebSocket requests BEFORE the emulated CPU starts, so
    memory reads issued right after ``session(action='start')`` fail with
    "CPU not started" until the ISO finishes booting. ``wait_ready`` polls
    a probe read until the CPU is up; if the probe never succeeds within
    the budget the session is likely WEDGED (not merely slow — observed
    wedge family: GPU backend failure records make the CPU never start,
    pc=0/ticks=0 forever). The hint routes the agent to log triage +
    session restart instead of retrying reads forever.
    """

    code = "BOOT_TIMEOUT"


class ScriptNotFound(ToolError):
    """Script name not in manifest."""

    code = "SCRIPT_NOT_FOUND"


class ScriptContractError(ToolError):
    """Script does not conform to Pydantic + ctx IoC contract."""

    code = "SCRIPT_CONTRACT_ERROR"


class ManifestError(ToolError):
    """scripts.manifest.yaml is malformed or references missing scripts."""

    code = "MANIFEST_ERROR"


class AddrInvalid(ToolError):
    """Address is invalid for the requested conversion mode."""

    code = "ADDR_INVALID"


class ScanNoMatch(ToolError):
    """Memory scan produced no matches."""

    code = "SCAN_NO_MATCH"


class CaptureEmpty(ToolError):
    """Dump/screenshot strategy produced no image (nothing bound yet)."""

    code = "CAPTURE_EMPTY"


class ProtectedAddress(ToolError):
    """Write target overlaps a protected code/kernel range (force=True overrides)."""

    code = "PROTECTED_ADDRESS"


class StepInvalid(ToolError):
    """Batch step argument failed structural validation (type/field/value)."""

    code = "STEP_INVALID"


class NotImplemented(ToolError):
    """Requested action is not yet implemented."""

    code = "NOT_IMPLEMENTED"


class IrEncodingDetected(ToolError):
    """read_u32 returned IR encoding — caller must use disassemble instead."""

    code = "IR_ENCODING_DETECTED"


class VerifyMismatch(ToolError):
    """Disassembled instruction does not match expected."""

    code = "VERIFY_MISMATCH"


class BreakpointError(ToolError):
    """Breakpoint operation failed."""

    code = "BREAKPOINT_ERROR"


class RateLimitExceeded(ToolError):
    """Rate limit exceeded for this tool."""

    code = "RATE_LIMIT_EXCEEDED"


class CpuStateError(ToolError):
    """CPU state precondition violated for a WS event.

    Raised by DebugClient (batch 1) when a REQUIRED_RUNNING event
    (gpu.stats.get / gpu.record.dump) is invoked while CPU is stepping.
    PPSSPP would otherwise let the ticketed wait time out (no GPU flip
    while paused) — the pre-check converts the silent timeout into a
    clean CpuStateError with a diagnostic message derived from the
    WS event contract's diagnostic_hint.
    """

    code = "CPU_STATE_ERROR"


class PortConflict(ToolError):
    """The WebSocket port is already in use by another active session.

    Raised by SessionManager.start_session when a new session's port
    conflicts with an existing active session's port. The launcher
    uses random ports by default, but fixed-port configurations can
    collide.
    """

    code = "PORT_CONFLICT"


class WsTimeout(ToolError):
    """A WebSocket call timed out waiting for a PPSSPP response.

    Raised when a ticketed RPC (WsTransport.call) or broadcast wait
    exceeds its timeout. Often indicates the CPU was in the wrong
    state (e.g., REQUIRED_RUNNING event called while CPU stepping —
    PPSSPP never produces the event, so the ticket times out).
    """

    code = "WS_TIMEOUT"


class WsDisconnected(ToolError):
    """The WebSocket connection was lost or was never established.

    Raised when WsTransport.call is invoked while ws is None or not
    in OPEN state, or when the recv loop detects ConnectionClosed.
    """

    code = "WS_DISCONNECTED"


class CpuFreezeSuspected(ToolError):
    """CPU freeze suspected — PPSSPP process alive but CPU not progressing.

    Raised by ``to_tool_error`` when a ticketed RPC (``WsTransport.call``)
    or ``pause()`` times out AND the PPSSPP process is still alive (PID
    alive) AND/OR game state indicates ``running``. This is a *suspicion*
    diagnosis, not a confirmed freeze — the LLM agent should perform
    further diagnosis (``ppsspp_screenshot``, ``hle.thread.list``,
    ``step(action='resume')`` attempt) rather than immediately restarting
    the session.

    Distinguished from ``WsDisconnected`` (PID dead = real disconnect)
    and ``CpuStateError`` (game paused = caller-side misuse). The
    ``CPU_FREEZE_SUSPECTED`` code lets agents choose the diagnose-then-
    recover path instead of the restart-session path.

    The message SHALL include (when available):
    - operation name (e.g. ``"pause"``, ``"gpu.stats.get"``)
    - PID status (``"PID alive"`` / ``"PID dead"``)
    - last known ``cpu.status.ticks`` value
    - game state (``loading`` / ``running`` / ``paused`` / ``quit``)
    - recovery hint: ``ppsspp_screenshot`` / ``step(action='resume')`` /
      ``hle.thread.list``
    """

    code = "CPU_FREEZE_SUSPECTED"


class PpssppProtocolError(ToolError):
    """PPSSPP returned an error event in response to a WS call.

    Raised when the recv loop receives ``{"event": "error", ...}`` for
    a ticketed call. The original PPSSPP error message is preserved
    in the exception message, and a diagnostic_hint is appended when
    the error matches a known PPSSPP protocol limitation.
    """

    code = "PPSSPP_PROTOCOL_ERROR"


class StepNoAdvanceError(ToolError):
    """Step commands were consumed but the CPU
    did not advance (pc/ticks unchanged after repeated attempts).

    Raised by ``PpssppDebugClient._step_with_retry`` after
    ``_STEP_MAX_NO_ADVANCE`` no-op broadcasts. This is a CPU-STATE
    diagnosis ("PPSSPP is not consuming steps at this state — use a
    breakpoint or step action=run_until"), NOT a disconnect — the previous
    reuse of ``SteppingFailedError`` (a RuntimeError without pid_alive)
    was misclassified by ``to_tool_error`` as ``WsDisconnected`` with a
    misleading "check that PPSSPP is running / reconnect" hint.

    Being a ToolError subclass, it passes through ``to_tool_error``
    unchanged so the client sees code STEP_NO_ADVANCE and the actionable
    message verbatim.
    """

    code = "STEP_NO_ADVANCE"


class StepOutError(ToolError):
    """step_out returned an invalid result.

    Raised by ``PpssppDebugClient.step_out`` when the returned ``pc``
    falls outside the PSP executable code range
    (``0x08800000``–``0x0C000000``). This typically indicates the
    stack walk returned an invalid caller frame (e.g. ``0x08000000``
    PSP user-memory base address as a stack-bottom sentinel) and the
    broadcast consumed was a stale one rather than the actual
    step-out completion. Surfacing this as a distinct error lets callers
    distinguish "stepped out to an invalid location" from "step_out
    timed out" so they can decide whether to retry or abort.
    """

    code = "STEP_OUT_ERROR"


class SteppingFailedError(RuntimeError):
    """Raised when with_stepping cannot enter stepping state.

    Pause failure is the canonical cause — CPU state is unknown and
    safe queries (safe_get_pc / safe_get_threads) cannot proceed without
    risking returning untrustworthy data marked as HIGH trust.

    Raising (rather than silently continuing the body) preserves the
    TrustLevel contract: callers of safe_get_pc rely on the documented
    HIGH trust level being accurate, which requires the CPU to actually
    be in stepping state when the query runs.

    Defined in errors.py (not stepping.py) so that to_tool_error can
    reference it without circular imports, and to keep all business
    exception types in one place.

    The optional ``pid_alive`` attribute carries the result of
    ``is_pid_alive(pid)`` when ``pause()`` performed a PID pre-check
    (``self._pid`` was set). It enables ``to_tool_error`` to
    distinguish:
    - ``pid_alive=True``  → CpuFreezeSuspected (PID alive but CPU not
      entering STEPPING — suspected freeze)
    - ``pid_alive=False`` → WsDisconnected (PID dead = real disconnect)
    - ``pid_alive=None``  → fall back to ``__cause__`` type (default)
    """

    def __init__(
        self,
        message: str,
        *,
        pid_alive: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.pid_alive = pid_alive


# ── Translation table ───────────────────────────────────────────────────


# Known PPSSPP protocol error patterns → diagnostic hints.
# Maps substrings of PPSSPP error messages to human-readable hints.
_PPSSPP_ERROR_HINTS: dict[str, str] = {
    "missing end parameter": (
        "Hint: memory.disasm requires count>0. If count=0 was passed, "
        "the MCP tool should have short-circuited it. This may "
        "indicate a tool wrapper bug."
    ),
    "missing parameter": (
        "Hint: a required WS parameter was not provided. Check the "
        "tool wrapper's parameter forwarding logic."
    ),
    "invalid address": (
        "Hint: the address is outside PSP user memory range "
        "(0x08800000-0x0C000000). Use convert_address to translate "
        "IDA addresses to PSP addresses."
    ),
    "not connected": (
        "Hint: PPSSPP WebSocket is not connected. Start a session "
        "first via ppsspp_session(action='start')."
    ),
    "not started": (
        "Hint: the emulated CPU has not started yet — PPSSPP answers "
        "WebSocket requests before the ISO finishes booting. Call "
        "ppsspp_session(action='wait_ready', session_id=...) and retry "
        "after it returns ready."
    ),
}


# ── PID / game-state resolvers for to_tool_error ──────────────────────
#
# Backed by contextvars.ContextVar rather than module-level globals: each
# asyncio task (tool call) gets its own resolver scope, set by
# session_client_with_transport (accept phase). Module-level state set by
# start_session would leak resolvers across concurrently running sessions.
_pid_resolver: contextvars.ContextVar[Callable[[], int | None] | None] = contextvars.ContextVar(
    "_pid_resolver", default=None
)
_game_state_resolver: contextvars.ContextVar[Callable[[], str | None] | None] = (
    contextvars.ContextVar("_game_state_resolver", default=None)
)

# Token type returned by set_error_context (used by reset_error_context).
ErrorContextToken = tuple[contextvars.Token, contextvars.Token]


def set_error_context(
    pid_resolver: Callable[[], int | None] | None,
    game_state_resolver: Callable[[], str | None] | None,
) -> ErrorContextToken:
    """Inject PID + game-state resolvers used by ``to_tool_error``.

    Backed by contextvars.ContextVar so each asyncio task (tool call)
    gets its own resolver scope. Called by
    ``session_client_with_transport`` (accept phase) rather than
    ``SessionManager.start_session`` — the resolvers are scoped to the
    tool-call task, not the session-start task, avoiding cross-session
    pollution when multiple sessions run concurrently.

    Returns:
        Token to pass to ``reset_error_context`` for cleanup.
    """
    t1 = _pid_resolver.set(pid_resolver)
    t2 = _game_state_resolver.set(game_state_resolver)
    return (t1, t2)


def reset_error_context(token: ErrorContextToken) -> None:
    """Reset PID + game-state resolvers to their previous values.

    Call when exiting the tool-call scope (e.g. in
    ``session_client_with_transport`` ``__aexit__``) so the contextvar
    does not leak to subsequent tool calls.
    """
    t1, t2 = token
    _pid_resolver.reset(t1)
    _game_state_resolver.reset(t2)


def _resolve_pid_alive() -> bool | None:
    """Best-effort PID liveness probe.

    Returns:
        - True if a PID context is set AND the PID is alive.
        - False if a PID context is set AND the PID is dead.
        - None if no PID context is set (caller falls back to legacy
          conservative translation).
    """
    pid_resolver = _pid_resolver.get()
    if pid_resolver is None:
        return None
    try:
        pid = pid_resolver()
    except Exception:
        return None
    if pid is None:
        return None
    try:
        # Module-attribute access so tests patching proc.is_pid_alive
        # take effect at this call site.
        return proc.is_pid_alive(pid)
    except Exception:
        return None


def _resolve_game_state() -> str | None:
    """Best-effort game-state query.

    Returns:
        - One of ``"loading" / "running" / "paused" / "quit"`` if a
          GameStateObserver is configured.
        - None if no observer is configured (caller falls back to
          legacy conservative translation).
    """
    game_state_resolver = _game_state_resolver.get()
    if game_state_resolver is None:
        return None
    try:
        return game_state_resolver()
    except Exception:
        return None


def to_tool_error(exc: Exception) -> ToolError:
    """Translate any exception to a ToolError.

    Exception translation: protocol/transport patterns to typed errors
    (decision 8/9):

    - ``SteppingFailedError``: two-signal judgment based on
      ``__cause__`` type + ``exc.pid_alive`` attribute:
      - ``__cause__`` is ``ConnectionError`` (incl.
        ``ConnectionRefusedError``) OR ``pid_alive=False`` →
        ``WsDisconnected`` (real disconnect).
      - ``pid_alive=True`` → ``CpuFreezeSuspected`` (PID alive but CPU
        not entering STEPPING — suspected freeze).
      - ``pid_alive=None`` + ``__cause__`` is ``TimeoutError`` →
        ``CpuFreezeSuspected`` (default conservative — without PID
        context, treat timeout-during-pause as suspected freeze).
      - Other → ``WsDisconnected`` (conservative).
    - ``asyncio.TimeoutError`` / ``TimeoutError``: comprehensive
      judgment based on PID + game state:
      - PID dead → ``WsDisconnected`` (real disconnect).
      - PID alive + game ``running`` → ``CpuFreezeSuspected``
        (suspected freeze: CPU death-loop / HLE block / GPU pipeline
        stall).
      - PID alive + game ``paused`` → ``CpuStateError`` (caller-side
        misuse: ticketed call should not be invoked while paused).
      - PID alive + game ``quit`` → ``WsDisconnected`` (game exited).
      - PID alive + game ``loading`` → ``WsTimeout`` (large file load,
        preserve existing behavior).
      - PID alive + game state unknown (observer not available) →
        ``WsTimeout`` (conservative).
      - PID context not available (no PID) → ``WsTimeout`` (preserve
        existing behavior).
    - ``ConnectionError`` (incl. ``ConnectionRefusedError``, and the
      recv-loop's plain ``ConnectionError``) → ``WsDisconnected`` with
      hint to start a session. Plain ``ConnectionError`` (the recv
      loop's failure mode for pending futures) is classified too, not
      just ``ConnectionRefusedError``.
    - ``RuntimeError("WebSocket not connected")`` → ``WsDisconnected``.
    - ``RuntimeError("PPSSPP error: ...")`` → ``PpssppProtocolError``
      with matched diagnostic hint from ``_PPSSPP_ERROR_HINTS``.
    - Other ``RuntimeError`` → ``CpuStateError`` if message matches
      stepping-related patterns, else ``INTERNAL``.
    - If `exc` is already a ToolError subclass, preserve it as-is.

    Args:
        exc: The exception to translate.

    Returns:
        A ToolError (or subclass) with a meaningful code and message.
    """
    if isinstance(exc, ToolError):
        # Preserve the original instance + its class-level `code`.
        return exc

    # SteppingFailedError: pause failed — two-signal judgment based on
    # __cause__ type + pid_alive attribute. Must be checked before the
    # generic "stepping" pattern match below, because SteppingFailedError's
    # message contains "stepping" but the correct diagnosis depends on
    # whether PPSSPP is unreachable (WsDisconnected) or alive-but-frozen
    # (CpuFreezeSuspected).
    if isinstance(exc, SteppingFailedError):
        cause = exc.__cause__
        pid_alive = getattr(exc, "pid_alive", None)

        # Signal 1: __cause__ is ConnectionError → real disconnect,
        # regardless of pid_alive (transport-level failure wins).
        if isinstance(cause, ConnectionError):
            return WsDisconnected(
                f"{exc} — Hint: pause failed, PPSSPP WebSocket "
                f"connection lost. Check that PPSSPP is running and "
                f"the session is connected, then retry."
            )

        # Signal 2: pid_alive attribute (set by pause() PID pre-check).
        if pid_alive is False:
            return WsDisconnected(
                f"{exc} — Hint: pause failed, PID dead; PPSSPP "
                f"process no longer running. The session is dead; "
                f"stop and restart it."
            )
        if pid_alive is True:
            return CpuFreezeSuspected(
                f"{exc} — Hint: pause failed, PID alive but CPU not "
                f"entering STEPPING; suspected CPU freeze. Consider "
                f"ppsspp_screenshot to capture current state, "
                f"step(action='resume') to attempt unfreeze, or "
                f"hle.thread.list to check thread status."
            )

        # pid_alive is None (pause() did not perform PID pre-check, e.g.
        # self._pid was None). Fall back to __cause__ type only.
        if isinstance(cause, (TimeoutError, asyncio.TimeoutError)):
            # Default conservative: timeout during pause without PID
            # context — suspected freeze (CpuFreezeSuspected).
            return CpuFreezeSuspected(
                f"{exc} — Hint: pause timed out, CPU state unknown "
                f"(no PID context); suspected CPU freeze. Consider "
                f"ppsspp_screenshot, step(action='resume'), or "
                f"hle.thread.list."
            )

        # Other / no cause: conservative WsDisconnected (legacy behavior).
        return WsDisconnected(
            f"{exc} — Hint: pause failed, likely because PPSSPP is "
            f"unreachable or the WebSocket connection dropped. Check "
            f"that PPSSPP is running and the session is connected, "
            f"then retry."
        )

    msg = str(exc) or repr(exc)
    msg_lower = msg.lower()

    # asyncio.TimeoutError / TimeoutError → comprehensive judgment.
    # Since Python 3.11, asyncio.TimeoutError IS built-in TimeoutError
    # (the names are aliases of the same class), so one isinstance
    # suffices on this project's >=3.13 floor.
    if isinstance(exc, TimeoutError):
        return _translate_timeout_error(exc, msg)

    # ConnectionError → WsDisconnected.
    # The recv loop fails pending futures with
    # plain ConnectionError("PPSSPP WebSocket disconnected") — previously
    # only ConnectionRefusedError was classified, so a mid-call disconnect
    # surfaced as generic INTERNAL instead of WS_DISCONNECTED.
    if isinstance(exc, ConnectionError) or "websocket not connected" in msg_lower:
        return WsDisconnected(
            f"{msg} — Hint: PPSSPP WebSocket is not connected. Start a "
            f"session first via ppsspp_session(action='start'), or "
            f"check that PPSSPP is running with --debugger-port."
        )

    # PPSSPP error event → PpssppProtocolError with hint.
    if "ppsspp error" in msg_lower:
        hint = _match_ppsspp_hint(msg_lower)
        return PpssppProtocolError(f"{msg}{hint}" if hint else msg)

    # RuntimeError stepping-related → CpuStateError.
    # Two directions based on what the CPU state IS and what it NEEDS:
    # - "not stepping" / "not paused" / "cpu is running" → CPU is running,
    #   needs pause → pause hint
    # - "is stepping" / generic "stepping" → CPU is paused, needs resume
    #   → resume hint
    # Order matters: specific negation patterns checked before the generic
    # "stepping" catch-all to avoid misclassifying "CPU not stepping"
    # (which means CPU IS running, needs pause) as "needs resume".
    if "not stepping" in msg_lower or "not paused" in msg_lower or "cpu is running" in msg_lower:
        return CpuStateError(
            f"{msg} — Hint: this operation requires CPU stepping. "
            f"Use step(action='pause') to pause the CPU before retrying."
        )
    # W18 (review v2): the bare "stepping" catch-all translated ANY
    # error message that happened to contain the word (a diagnostic
    # script's own failure, a third-party warning) into CPU_STATE_ERROR
    # with a "resume" hint — steering the agent to the wrong remedy.
    # Only message shapes that actually mean "CPU is paused" get the
    # hint now; everything else falls through to the generic wrap.
    if "is stepping" in msg_lower or "cpu is stepping" in msg_lower:
        return CpuStateError(
            f"{msg} — Hint: this operation requires CPU running. "
            f"Use step(action='resume') to resume the CPU before retrying."
        )

    # Default: wrap in generic ToolError.
    return ToolError(msg)


def _translate_timeout_error(
    exc: Exception,
    msg: str,
) -> ToolError:
    """Decision 9: translate TimeoutError via PID + game-state judgment.

    Decision matrix (see cpu-freeze-detection/spec.md):
    - PID dead → WsDisconnected (real disconnect).
    - PID alive + game ``running`` → CpuFreezeSuspected (suspected
      freeze: CPU death-loop / HLE block / GPU pipeline stall).
    - PID alive + game ``paused`` → CpuStateError (caller misuse).
    - PID alive + game ``quit`` → WsDisconnected (game exited).
    - PID alive + game ``loading`` → WsTimeout (preserve existing).
    - PID alive + game state unknown (observer not available) →
      WsTimeout (conservative).
    - PID context not available (no PID) → WsTimeout (preserve existing).
    """
    pid_alive = _resolve_pid_alive()

    # PID context not available: preserve existing WsTimeout behavior.
    if pid_alive is None:
        return WsTimeout(
            f"{msg} — Hint: ticketed RPC timed out. For REQUIRED_RUNNING "
            f"events (gpu.stats.get, gpu.record.dump), ensure CPU is "
            f"running (not stepping). Use step(action='resume') to "
            f"resume the CPU before retrying."
        )

    # PID dead → real disconnect.
    if pid_alive is False:
        return WsDisconnected(
            f"{msg} — Hint: timed out, PID dead; PPSSPP process no "
            f"longer running. Stop and restart the session."
        )

    # PID alive → game state decides.
    game_state = _resolve_game_state()

    if game_state == "running":
        return CpuFreezeSuspected(
            f"{msg} — Hint: timed out, PID alive + game running, "
            f"CPU freeze suspected (death-loop / HLE block / GPU "
            f"pipeline stall). Consider ppsspp_screenshot or "
            f"hle.thread.list."
        )
    if game_state == "paused":
        return CpuStateError(
            f"{msg} — Hint: timed out, game paused; ticketed call "
            f"should not be invoked while paused. Use "
            f"step(action='resume') to resume the CPU before retrying."
        )
    if game_state == "quit":
        return WsDisconnected(
            f"{msg} — Hint: timed out, game quit; PPSSPP process "
            f"alive but game exited. Stop and restart the session."
        )

    # game loading OR game state unknown (observer not available):
    # preserve existing WsTimeout behavior (conservative).
    if game_state == "loading":
        return WsTimeout(
            f"{msg} — Hint: timed out, game loading (large file "
            f"load may be slow); retry after loading completes."
        )
    # game_state is None (observer not configured) → conservative.
    return WsTimeout(
        f"{msg} — Hint: ticketed RPC timed out. For REQUIRED_RUNNING "
        f"events (gpu.stats.get, gpu.record.dump), ensure CPU is "
        f"running (not stepping). Use step(action='resume') to "
        f"resume the CPU before retrying."
    )


def _match_ppsspp_hint(msg_lower: str) -> str:
    """Match a PPSSPP error message against known patterns.

    Returns the diagnostic hint string if a pattern matches, else "".
    """
    for pattern, hint in _PPSSPP_ERROR_HINTS.items():
        if pattern in msg_lower:
            return f" — {hint}"
    return ""
