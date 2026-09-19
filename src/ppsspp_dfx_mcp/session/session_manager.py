"""Session manager orchestration.

Business logic for start/stop/get/list sessions. Persists session state
to ~/.ppsspp-dfx/sessions.json with atomic write (tmp + os.replace()).
Idle GC: every 60s, sessions idle for > 1800s are auto-stopped.

Thread-safety: load-modify-save sequences are protected by `asyncio.Lock`
so concurrent tool calls cannot lose writes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ppsspp_dfx_mcp.config import (
    boot_heal_quarantine_gpu_blacklist,
    sessions_path,
    test_mode,
    ws_host,
    ws_port,
)
from ppsspp_dfx_mcp.core import proc
from ppsspp_dfx_mcp.core.launcher import PpssppLauncher, _force_kill_pid
from ppsspp_dfx_mcp.errors import (
    BootTimeout,
    IsoNotFound,
    PortConflict,
    SessionExpired,
    SessionNotFound,
)
from ppsspp_dfx_mcp.models.session import Session

if TYPE_CHECKING:
    # Type-only imports to avoid circular dependencies at runtime. WsTransport
    # is lazily imported in start_session / _probe_ws_connection; GameStateObserver
    # is lazily imported in start_session.
    from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver
    from ppsspp_dfx_mcp.core.transport import WsTransport
import contextlib

from ppsspp_dfx_mcp.session.safe_boot import (
    DEFAULT_PROBE_ADDR,
    probe_cpu_ready,
    quarantine_gpu_backend_blacklist,
    wedge_cooldown,
)

# Idle GC threshold (seconds). Sessions inactive for this long are auto-stopped.
IDLE_GC_THRESHOLD_S = 1800
# Idle GC scan interval (seconds).
IDLE_GC_INTERVAL_S = 60

# Fields that _load_sessions passes to Session(). Unknown keys in
# sessions.json (e.g. "launcher" from a manual edit or future version)
# are silently dropped instead of causing TypeError.
_SESSION_FIELDS = frozenset(
    {
        "session_id",
        "iso_path",
        "pid",
        "ws_url",
        "created_at",
        "last_active_at",
        "exec_count",
        "ws_connected",
        "extra",
    }
)

log = logging.getLogger(__name__)


def _parse_dt(value: Any) -> datetime:
    """Parse a value into a timezone-aware datetime.

    Accepts a `datetime` (returned as-is) or an ISO 8601 string (parsed).
    Falls back to now(utc) on parse failure so a corrupt sessions.json
    entry doesn't crash the GC loop.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _load_sessions() -> dict[str, Session]:
    """Load all sessions from the JSON file. Returns empty dict if missing."""
    path = sessions_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("sessions.json malformed, starting fresh: %s", e)
        return {}
    if not isinstance(data, dict):
        log.warning(
            "sessions.json root is not a dict (got %r); starting fresh", type(data).__name__
        )
        return {}
    result: dict[str, Session] = {}
    for sid, sess_dict in data.items():
        if isinstance(sess_dict, dict):
            try:
                # Filter to known fields only — unknown keys (e.g. "launcher"
                # from a manual edit or future version) are silently dropped.
                filtered = {k: v for k, v in sess_dict.items() if k in _SESSION_FIELDS}
                # Convert ISO strings → datetime (Session model uses datetime).
                filtered["created_at"] = _parse_dt(sess_dict.get("created_at"))
                filtered["last_active_at"] = _parse_dt(sess_dict.get("last_active_at"))
                sess = Session(**filtered)
                # Runtime provenance — this Session was materialized from
                # sessions.json, NOT created by start_session in this
                # process. Resources/tools surface the flag so agents can
                # tell a restored session from a fresh one.
                sess = dataclasses.replace(sess, extra={**sess.extra, "restored": True})
                result[sid] = sess
            except TypeError:
                continue
    return result


def _save_sessions(sessions: dict[str, Session]) -> None:
    """Persist sessions to JSON with atomic write (tmp + os.replace).

    Single-writer model: no concurrent access; os.replace() guards against
    process crash mid-write leaving a half-written file. The tmp file keeps
    the `.json` suffix (sessions.json.tmp) so tooling that keys on extension
    still recognizes it.
    """
    path = sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {sid: _session_to_dict(s) for sid, s in sessions.items()}
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


async def _load_sessions_async() -> dict[str, Session]:
    """Async wrapper around _load_sessions for use in async contexts.

    sessions.json is normally a small file (< 100KB), but on network
    filesystems or sandboxed environments the I/O may take significantly
    longer. Wrapping in asyncio.to_thread prevents blocking the event
    loop, which is critical when multiple sessions are active — a slow
    file read on one session should not block tool calls on others.
    """
    return await asyncio.to_thread(_load_sessions)


async def _save_sessions_async(sessions: dict[str, Session]) -> None:
    """Async wrapper around _save_sessions for use in async contexts.

    See _load_sessions_async for rationale.
    """
    await asyncio.to_thread(_save_sessions, sessions)


def _session_to_dict(sess: Session) -> dict[str, Any]:
    """Convert a Session to a JSON-serializable dict.

    Datetime fields are serialized as ISO 8601 strings (JSON has no
    native datetime type). Inverse of `_parse_dt` in `_load_sessions`.
    """
    return {
        "session_id": sess.session_id,
        "iso_path": sess.iso_path,
        "pid": sess.pid,
        "ws_url": sess.ws_url,
        "created_at": sess.created_at.isoformat(),
        "last_active_at": sess.last_active_at.isoformat(),
        "exec_count": sess.exec_count,
        "ws_connected": sess.ws_connected,
        "extra": sess.extra,
    }


def _pid_name_is_ppsspp(pid: int) -> bool | None:
    """Best-effort process-name check for kill-safety.

    sessions.json can carry a stale PID that the OS has since REUSED for
    an unrelated process; force-killing it (taskkill /F /T on Windows)
    would take down that process tree. This helper checks whether the
    image name looks like PPSSPP.

    Returns:
        True/False — name could be determined (Windows:
        QueryFullProcessImageNameW; Linux: /proc/<pid>/comm).
        None — name could not be determined (caller keeps the legacy
        kill-anyway behavior).
    """
    if sys.platform == "win32":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None  # cannot even open — let liveness decide
            try:
                size = ctypes.c_ulong(512)
                buf = ctypes.create_unicode_buffer(512)
                ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
                if not ok:
                    return None
                return Path(buf.value).name.lower().startswith("ppsspp")
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    try:
        comm = (
            (Path("/proc") / str(pid) / "comm")
            .read_text(encoding="utf-8", errors="replace")
            .strip()
            .lower()
        )
        return comm.startswith("ppsspp") if comm else None
    except OSError:
        return None


def _kill_stale_pid_safe(pid: int) -> None:
    """Force-kill a stale-session PID unless the name check refuses.

    A False name check (PID reused by a non-PPSSPP process) logs
    a warning and refuses the kill; None (indeterminate) preserves the
    best-effort kill.
    """
    name_check = _pid_name_is_ppsspp(pid)
    if name_check is False:
        log.warning(
            "refusing to kill PID %d: process image is not PPSSPP "
            "(stale sessions.json entry with a reused PID?)",
            pid,
        )
        return
    _force_kill_pid(pid)


def _idle_seconds(sess: Session) -> float:
    """Compute seconds since last_active_at."""
    try:
        now = datetime.now(UTC)
        return (now - sess.last_active_at).total_seconds()
    except (TypeError, ArithmeticError):
        return float("inf")


class _BootWedge(Exception):
    """Internal: a resilient-start launch attempt showed wedge evidence.

    Never escapes start_session — the resilient loop converts it into a
    heal-and-relaunch cycle and, on exhaustion, a BootTimeout for the
    caller. Kept private to the session layer (tools must not catch it).
    """


def _version_fingerprint(transport: Any) -> dict[str, Any]:
    """Compact PPSSPP build fingerprint from the version
    handshake (reason/relatedAddress presence differs across dev builds).
    Unknown shapes degrade to a raw truncation."""
    info = getattr(transport, "version_info", None) or {}
    keys = ("name", "version", "sdkVersion", "revision", "gitHash", "build")
    fp = {k: info[k] for k in keys if k in info}
    if not fp:
        fp = {"raw": str(info)[:200]}
    return fp


class _ReentrantSessionLock:
    """Per-session tool-call lock with same-task reentrancy.

    Cross-task exclusion is unchanged — one task at a time owns the
    session, so concurrent tool calls still serialize (or fail with
    SessionBusy after SESSION_BUSY_TIMEOUT_S, exactly as before). What
    changed: a task that already owns the lock may acquire it again
    without blocking, counted by depth.

    Why: a batch_step holds the lock for its whole body, and its
    embedded screenshot step opens session_client again in the SAME
    task. With a plain asyncio.Lock that nested acquire deadlocked
    against itself and failed with SESSION_BUSY after the 5s wait —
    the documented step type was unconditionally unusable (D3).
    Same-task reentry preserves every exclusion guarantee while
    letting nested tool calls through.

    release() must be called by the owning task (enforced), and the
    acquire/wait protocol stays compatible with
    ``asyncio.wait_for(lock.acquire(), timeout=...)`` — cancellation
    while waiting leaves ownership untouched.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0

    async def acquire(self) -> None:
        task = asyncio.current_task()
        if task is not None and self._owner is task:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = asyncio.current_task()
        self._depth = 1

    def release(self) -> None:
        if self._depth == 0 or self._owner is not asyncio.current_task():
            raise RuntimeError(
                "session lock released by a task that does not own it"
            )
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()

    def locked(self) -> bool:
        return self._owner is not None

    def __repr__(self) -> str:
        return (
            f"<_ReentrantSessionLock owner={self._owner!r} depth={self._depth}>"
        )


class SessionManager:
    """Session lifecycle manager with per-session launcher ownership.

    Each session owns its own `PpssppLauncher` instance. `stop_session`
    only stops that session's launcher, leaving other sessions' launchers
    untouched. Session metadata is persisted to sessions.json; launcher
    instances are runtime-only and tracked in `_launchers` (in-memory
    dict keyed by session_id) so that `stop_session` can find the live
    launcher even though `_load_sessions()` returns sessions without
    launchers (the launcher is not JSON-serializable).

    Thread-safety: an `asyncio.Lock` protects all load-modify-save
    sequences so concurrent tool calls cannot lose writes. The lock
    must be acquired before any `_load_sessions() → modify → _save_sessions()`
    compound operation.
    """

    def __init__(self) -> None:
        # session_id -> launcher, for sessions started in this process.
        # Sessions loaded from disk have no launcher here (the process is
        # gone or was started by a previous MCP server run). stop_session
        # falls back to _force_kill_pid for those.
        self._launchers: dict[str, PpssppLauncher] = {}
        # session_id -> session-level WsTransport. Mirrors the _launchers
        # pattern: in-memory only, not persisted to sessions.json.
        # Populated by start_session; closed by stop_session. Sessions
        # loaded from disk have no transport (process is gone) —
        # stop_session skips the close for those and falls back to
        # _force_kill_pid.
        self._transports: dict[str, WsTransport] = {}
        # session_id -> GameStateObserver. Same lifecycle as _transports:
        # created in start_session, stopped in stop_session. In-memory only.
        self._observers: dict[str, GameStateObserver] = {}
        # Per-session serialization for TOOL CALLS.
        # The MCP SDK dispatches requests concurrently (each request runs in
        # its own task), while the session-level WsTransport, the
        # GameStateObserver per-event queues, and the global CPU stepping
        # state are all single-consumer. client_helper acquires this lock for
        # the whole tool-call body; a second concurrent call either queues
        # briefly or fails with SessionBusy (see SESSION_BUSY_TIMEOUT_S).
        # stop_session / gc_idle_sessions pop the entry alongside the
        # transport/observer.
        self._session_locks: dict[str, _ReentrantSessionLock] = {}
        # Protects load-modify-save sequences against concurrent writes.
        self._lock = asyncio.Lock()

    def session_lock(self, session_id: str) -> "_ReentrantSessionLock":
        """Return the per-session tool-call lock, creating it on first use.

        get-or-create without awaits, so it is atomic within the event loop.

        ── Lock contract ──────────────────────────────────────────────────
        HOLDING the lock: every tool that opens ``session_client`` or
        ``session_capture`` (i.e. everything that touches the session
        transport, the observer per-event queues, or the global CPU
        stepping state) — the lock wraps the WHOLE tool-call body. Also
        held by tools calling ``validate_session_alive``-only paths that
        go through session_client.

        PARTIAL_HOLD (lock-free wait, locked sub-ops):
        ``ppsspp_breakpoint(action="wait"/"trace")``
        acquire the lock ONLY for their short sub-operations (arm /
        probe / capture / cleanup); the breakpoint WAIT itself
        subscribes to the observer's cpu.stepping fan-out and holds NO
        lock, so concurrent reads/observes keep working during the
        wait. All CPU-state mutations (mem_bp_add/remove, resume,
        safe_get_pc) stay inside locked regions.

        NOT holding the lock (by design):
        - ``ppsspp_wait_frames`` — pure wall-clock sleep + liveness
          checks; touches no session resource (verified by AST tripwire
          test_lock_contract_tripwire.py).
        - ``ppsspp_session(action=wait_ready)`` — polls the CPU-start
          probe via raw session-level transport reads (lock-free, so
          health/list stay responsive during boot); no session_client.
        - ``ppsspp_analyze_log`` — no session interaction.
        - ``ppsspp_session(action=get)`` / ``ppsspp_session(action=list)`` —
          read-only health, must stay responsive while a tool call runs.
        - fake test mode — each call owns a private FakeTransport.
        - ``ppsspp_batch_status`` / ``ppsspp_batch_cancel`` —
          lock-free registry reads/cancels; polling MUST stay responsive
          while a background batch holds the session lock for its whole
          (possibly minutes-long) duration. A background batch holds the
          lock exactly like a foreground batch_step would — other tools
          see SESSION_BUSY (with a hint naming the batch id) until the
          job reaches a terminal state.

        Enforcement: tests/unit/l2_mcp_contract/
        test_lock_contract_tripwire.py scans every registered tool's AST
        and fails when a tool opens a session context without being in
        the documented HOLDING or PARTIAL_HOLD set.
        """
        return self._session_locks.setdefault(session_id, _ReentrantSessionLock())

    async def start_session(
        self,
        iso_path: str,
        *,
        resilient: bool = False,
        ready_timeout_s: float = 75.0,
        max_restarts: int = 2,
        probe_addr: int = DEFAULT_PROBE_ADDR,
    ) -> Session:
        """Start a new session (async).

        Creates a dedicated `PpssppLauncher` for this session, starts it
        (offloaded to a thread via `asyncio.to_thread` so the event loop
        is not blocked), and persists the new session to disk.

        In fake test mode (PPSSPP_DFX_TEST_MODE=fake), the launcher is
        skipped entirely — a fake session with `pid=None` and a
        sentinel `ws_url="fake://test"` is created so the test stack
        can substitute a FakeTransport for the real WsTransport.

        ``resilient=True`` turns this into a
        self-healing boot: each attempt runs the CPU-ready probe
        (session/safe_boot.probe_cpu_ready) after the WS bind, and wedge
        evidence (probe budget exhausted / handshake never accepted /
        process died mid-boot) triggers teardown → GPU-backend-blacklist
        quarantine (config-gated, rename-only) → relaunch with the SAME
        session_id, up to ``max_restarts`` retries. The returned Session
        carries ``extra["recovered"]`` (relaunch count, 0 = first
        attempt) and ``extra["ppsspp_version"]`` (build fingerprint).
        Exhausting all attempts raises ``BootTimeout`` and removes the
        dead session entry. Default ``resilient=False`` keeps the
        best-effort behavior. Fake mode ignores resilient entirely.

        Args:
            iso_path: Path to the ISO file.
            resilient: Enable wedge self-heal (default False).
            ready_timeout_s: Per-attempt CPU-ready budget (resilient only).
            max_restarts: Relaunch budget after the first wedge (resilient
                only; total launches = max_restarts + 1).
            probe_addr: Readiness probe address (resilient gate; default
                top.prx base 0x08804000).

        Returns:
            New Session domain model.

        Raises:
            IsoNotFound: ISO file does not exist (skipped in fake mode
                so tests can pass any path).
            PortConflict: another LIVE session owns the chosen port
                (dead-PID sessions are skipped by the conflict check).
            BootTimeout: resilient start exhausted all attempts.
        """
        iso = Path(iso_path).expanduser().resolve()
        if not iso.is_file() and test_mode() != "fake":
            raise IsoNotFound(f"ISO file not found: {iso}")

        session_id = str(uuid.uuid4())

        # ── Fake test mode: skip launcher, return a fake session ──
        if test_mode() == "fake":
            sess = Session(
                session_id=session_id,
                iso_path=str(iso),
                pid=None,
                ws_url="fake://test",
            )
            async with self._lock:
                sessions = await _load_sessions_async()
                sessions[session_id] = sess
                await _save_sessions_async(sessions)
            return sess

        if not resilient:
            return await self._start_once(
                session_id,
                iso,
                gate=False,
                ready_timeout_s=0.0,
                probe_addr=probe_addr,
                attempt=0,
            )

        # ── Resilient boot: heal-and-relaunch, stable session id ──
        attempts = max(0, int(max_restarts)) + 1
        last_wedge: _BootWedge | None = None
        quarantined: Path | None = None
        for attempt in range(attempts):
            try:
                return await self._start_once(
                    session_id,
                    iso,
                    gate=True,
                    ready_timeout_s=ready_timeout_s,
                    probe_addr=probe_addr,
                    attempt=attempt,
                )
            except _BootWedge as e:
                last_wedge = e
                log.warning(
                    "wedge heal: launch attempt %d/%d failed for session %s: %s",
                    attempt + 1,
                    attempts,
                    session_id,
                    e,
                )
                launcher_exe = await self._teardown_wedged_attempt(session_id)
                if boot_heal_quarantine_gpu_blacklist() and launcher_exe:
                    quarantined = quarantine_gpu_backend_blacklist(launcher_exe)
                if attempt + 1 < attempts:
                    await asyncio.to_thread(wedge_cooldown)
        await self._discard_session_entry(session_id)
        raise BootTimeout(
            f"emulated CPU did not start after {attempts} launch "
            f"attempt(s) (resilient start; GPU backend blacklist "
            f"quarantined: {quarantined}); last error: {last_wedge}. "
            f"Check ppsspp_analyze_log for boot errors, then stop and "
            f"restart the session."
        )

    async def _start_once(
        self,
        session_id: str,
        iso: Path,
        *,
        gate: bool,
        ready_timeout_s: float,
        probe_addr: int,
        attempt: int,
    ) -> Session:
        """One launch attempt.

        ``gate=False`` keeps the best-effort behavior (no
        readiness wait). ``gate=True`` (resilient) blocks on the
        CPU-ready probe and raises ``_BootWedge`` on wedge evidence so
        the caller can heal and relaunch with the same session_id.
        """
        # ── Production mode: launch real PPSSPP ──
        launcher = PpssppLauncher()
        try:
            proc = await launcher.start(iso)
        except Exception:
            # Clean up any partially-started process before propagating.
            await asyncio.to_thread(launcher.stop)
            raise

        # Use the launcher's actual chosen port (random or fixed).
        # Falls back to config ws_port() if launcher didn't pick one
        # (defensive — should not happen in normal flow).
        port = launcher.ws_port
        if port is None:
            port = ws_port()
        ws_url = f"ws://{ws_host()}:{port}/debugger"

        # Check for port conflicts with existing active sessions.
        # The launcher uses random ports by default, but when a fixed
        # port is configured (ws_port != None), multiple sessions may
        # collide. This check prevents the second session from silently
        # sharing the first session's PPSSPP process.
        #
        # The conflict check runs AFTER process launch because the
        # launcher's actual port is only known after start() (random
        # port selection). If PortConflict is raised, the already-
        # launched process is cleaned up before re-raising.
        async with self._lock:
            sessions = await _load_sessions_async()
            try:
                _check_port_conflict(sessions, session_id, port)
            except PortConflict:
                # Clean up the orphaned process before propagating.
                await asyncio.to_thread(launcher.stop)
                raise

            sess = Session(
                session_id=session_id,
                iso_path=str(iso),
                pid=proc.pid,
                ws_url=ws_url,
            )

            # Persist first, then track the launcher. If _save_sessions
            # fails, the launcher is NOT stored in _launchers, so it
            # won't leak — the process is still running but the session
            # is not on disk, and the caller gets the error. A future GC
            # cycle will reap the orphaned process via _force_kill_pid.
            sessions[session_id] = sess
            await _save_sessions_async(sessions)

        # Track the launcher in-memory so stop_session can find it later.
        # _save_sessions() does not persist the launcher (it is not
        # JSON-serializable), so _load_sessions() returns sessions without
        # a launcher. The in-memory dict is the single source of truth
        # for "which launcher belongs to this session_id in this process".
        self._launchers[session_id] = launcher

        # Proactively probe the WS connection so the
        # session returned to the caller reflects the real ws_connected
        # state. Without this, ws_connected stays False until the first
        # tool call opens a WS — confusing for callers that query
        # session(get) immediately after start_session.
        #
        # The probe is best-effort: any exception (including bugs in
        # _probe_ws_connection itself, e.g. an unexpected AttributeError
        # before its try/except) MUST NOT propagate out of start_session
        # — the session is already persisted and the caller would see a
        # spurious failure for what is purely a liveness hint. ws_connected
        # stays False on failure; the first tool call's session_client
        # will discover the true WS state.
        try:
            ws_connected = await _probe_ws_connection(ws_url)
        except Exception as e:
            log.warning("N-07 probe: unexpected exception (best-effort, swallowed): %s", e)
            ws_connected = False
        if ws_connected:
            async with self._lock:
                sessions = await _load_sessions_async()
                if session_id in sessions:
                    sess = sess.with_ws_connected(True)
                    sessions[session_id] = sess
                    await _save_sessions_async(sessions)

        # ── Session-level transport + observer ──
        # Best-effort: if establishment fails (e.g. no real PPSSPP in unit
        # tests with _StubLauncher), log warning and leave _transports /
        # _observers unpopulated — client_helper falls back to per-call
        # transport creation. Mirrors the probe's best-effort pattern.
        # Only attempted when the probe succeeded (WS is ready).
        #
        # set_error_context is NOT called here — it is owned by
        # session_client_with_transport so each tool-call task gets its
        # own contextvar scope, avoiding cross-session pollution when
        # multiple sessions run concurrently.
        #
        # Use local variables (transport / observer) for cleanup, so
        # that if connect() fails before
        # `self._transports[session_id] = transport` is assigned, the
        # transport object is still closed (no leak).
        if ws_connected:
            transport = None
            observer = None
            try:
                from ppsspp_dfx_mcp.core.game_state_observer import (
                    GameStateObserver,
                )
                from ppsspp_dfx_mcp.core.transport import WsTransport

                transport = WsTransport(ws_host(), port)
                await transport.connect()
                await transport.send_version()
                observer = GameStateObserver(transport)
                await observer.start()
                self._transports[session_id] = transport
                self._observers[session_id] = observer
                # Fold the version fingerprint in for
                # EVERY bound session (not only resilient starts) —
                # reason/relatedAddress presence differs across PPSSPP
                # builds, so this is the wire-evidence key.
                async with self._lock:
                    sessions = await _load_sessions_async()
                    cur = sessions.get(session_id)
                    if cur is not None:
                        extra = dict(cur.extra)
                        extra["ppsspp_version"] = _version_fingerprint(transport)
                        # _load_sessions() stamps restored=True on every
                        # record it reads — including the one this call just
                        # created and persisted (D11). This session was born
                        # in-process, so it is not a restored session.
                        extra["restored"] = False
                        sess = dataclasses.replace(cur, extra=extra)
                        sessions[session_id] = sess
                        await _save_sessions_async(sessions)
                transport = None  # ownership transferred; do not close on exit
                observer = None
            except Exception as e:
                log.warning(
                    "session-level transport establishment failed for "
                    "%s (best-effort, swallowed): %s",
                    session_id,
                    e,
                )
                # Clean up partially-established state using the local
                # variables: if connect() failed
                # before `self._transports[session_id] = transport`,
                # the local `transport` is still set and needs close.
                if observer is not None:
                    with contextlib.suppress(Exception):
                        await observer.stop()
                # Pop from dict if assigned (idempotent if never set).
                obs = self._observers.pop(session_id, None)
                if obs is not None and obs is not observer:
                    with contextlib.suppress(Exception):
                        await obs.stop()
                if transport is not None:
                    with contextlib.suppress(Exception):
                        await transport.close()
                transp = self._transports.pop(session_id, None)
                if transp is not None and transp is not transport:
                    with contextlib.suppress(Exception):
                        await transp.close()

        if gate:
            # Resilient-start readiness gate: wedge evidence here raises
            # _BootWedge — the resilient caller tears down, quarantines
            # the GPU blacklist, and relaunches with the SAME session_id.
            # The gate folds recovered/ppsspp_version into the persisted
            # record; the caller must return the UPDATED snapshot, not
            # the pre-gate object.
            updated = await self._gate_boot(session_id, ready_timeout_s, probe_addr, attempt)
            if updated is not None:
                sess = updated
        return sess

    async def _gate_boot(
        self,
        session_id: str,
        ready_timeout_s: float,
        probe_addr: int,
        attempt: int,
    ) -> Session | None:
        """Resilient-start readiness gate (raises _BootWedge on wedges).

        Wedge evidence, in decisiveness order: the PPSSPP process died
        mid-boot (alive_check), the probe budget exhausted (GPU backend
        blacklist / device-creation hang family), or the WS bind never
        happened (handshake-hang variant — transport is None).
        """
        transport = self._transports.get(session_id)
        if transport is None:
            raise _BootWedge(
                "WS probe/bind failed — the port listened but the "
                "debugger never accepted a handshake (device-creation-"
                "hang wedge variant)"
            )
        sess = await self.get_session_state(session_id)
        started = time.monotonic()
        try:
            await probe_cpu_ready(
                transport,
                probe_addr=probe_addr,
                budget_s=ready_timeout_s,
                alive_check=(lambda s=sess: s.pid is None or proc.is_pid_alive(s.pid)),
            )
        except BootTimeout as e:
            raise _BootWedge(str(e)) from e
        boot_s = time.monotonic() - started
        updated: Session | None = None
        async with self._lock:
            sessions = await _load_sessions_async()
            cur = sessions.get(session_id)
            if cur is not None:
                extra = dict(cur.extra)
                extra["recovered"] = attempt
                extra["ppsspp_version"] = _version_fingerprint(transport)
                # Same D11 rationale: the relaunch is in-process, so the
                # reloaded record's restored=True stamp is cleared.
                extra["restored"] = False
                updated = dataclasses.replace(cur, extra=extra)
                sessions[session_id] = updated
                await _save_sessions_async(sessions)
        log.info(
            "boot: session %s CPU ready in %.1fs (launch attempt %d, recovered=%d)",
            session_id,
            boot_s,
            attempt + 1,
            attempt,
        )
        return updated

    async def _teardown_wedged_attempt(self, session_id: str) -> Path | None:
        """Tear down a wedged launch attempt (stop_session minus the
        sessions.json removal — the retry reuses the same session_id).

        The kill is necessarily a force-kill: a wedged CPU cannot process
        a graceful resume. The GPU-backend blacklist the kill may poison
        is quarantined by the caller RIGHT AFTER this teardown (kill →
        quarantine order keeps the next launch clean).

        Returns:
            The launcher's exe_path (for blacklist quarantine), or None
            when no launcher was tracked.
        """
        async with self._lock:
            launcher = self._launchers.pop(session_id, None)
            transport = self._transports.pop(session_id, None)
            observer = self._observers.pop(session_id, None)
            self._session_locks.pop(session_id, None)
        if transport is not None:
            with contextlib.suppress(Exception):
                await transport.close()
        if observer is not None:
            with contextlib.suppress(Exception):
                await observer.stop()
        exe_path: Path | None = getattr(launcher, "exe_path", None)
        if launcher is not None:
            try:
                await asyncio.to_thread(launcher.stop)
            except Exception as e:
                log.warning("wedge heal: launcher.stop failed: %s", e)
        return exe_path

    async def _discard_session_entry(self, session_id: str) -> None:
        """Remove the sessions.json entry of an exhausted resilient start
        so callers observe clean state instead of a dead session."""
        async with self._lock:
            sessions = await _load_sessions_async()
            if sessions.pop(session_id, None) is not None:
                await _save_sessions_async(sessions)

    async def stop_session(self, session_id: str) -> Session:
        """Stop a session by ID (async).

        Three-phase locking protocol (with Phase 1.5 close) to avoid
        blocking other sessions during the (potentially slow)
        launcher.stop call:

        1. **Acquire lock briefly** to look up the session snapshot and
           pop the in-memory launcher / transport / observer. Release lock.
        1.5. **Outside the lock**, close the session-level transport +
           observer (task 4.4, close phase). Done BEFORE killing the
           process so transport operations don't fail on a dead process.
           Skipped for disk-loaded sessions (no transport/observer).
        2. **Outside the lock**, offload `launcher.stop()` (or
           `_force_kill_pid` for disk-loaded sessions) to a thread.
           Other sessions can start/stop concurrently during this phase.
        3. **Re-acquire lock briefly** to remove the session from
           sessions.json and persist.

        Trade-off: between phase 1 and phase 3, `list_sessions` may
        still see the stopping session (stale for a few seconds). This
        is acceptable — "stale for 5s" is strictly better than the
        previous behavior of holding the lock for 5+ seconds while
        `launcher.stop` runs, which blocked every other session
        operation (start/stop/touch) for the entire duration.

        Duplicate `stop_session` calls for the same session_id during
        phase 2 are safe: `launcher.stop` is idempotent (`_proc = None`
        after first call), and phase 3's `sessions.pop(session_id, None)`
        is also idempotent.

        Raises:
            SessionNotFound: session_id not in active sessions.
        """
        # Phase 1: brief lock to read session + pop launcher/transport/observer.
        async with self._lock:
            sessions = await _load_sessions_async()
            sess = sessions.get(session_id)
            if sess is None:
                raise SessionNotFound(f"session not found: {session_id}")
            launcher = self._launchers.pop(session_id, None)
            transport = self._transports.pop(session_id, None)
            observer = self._observers.pop(session_id, None)
            # Drop the per-session tool-call lock too — the session
            # is going away; in-flight tool calls holding the lock fail fast
            # on the closed transport and release it.
            self._session_locks.pop(session_id, None)
            sess_snapshot = sess

        # Phase 1.5: close session-level transport + observer. Done
        # BEFORE killing the process to avoid transport operations
        # failing after the process is gone. Skipped for disk-
        # loaded sessions (transport/observer are None — process was started
        # by a previous MCP server run).
        if observer is not None:
            try:
                await observer.stop()
            except Exception as e:
                log.warning("stop_session: observer.stop failed: %s", e)
        if transport is not None:
            try:
                await transport.close()
            except Exception as e:
                log.warning("stop_session: transport.close failed: %s", e)

        # Phase 2: outside the lock — launcher.stop can take 5+ seconds.
        # Other sessions' start/stop/touch proceed concurrently.
        if launcher is not None:
            await asyncio.to_thread(launcher.stop)
        elif sess_snapshot.pid is not None:
            # Disk-loaded session with no in-memory launcher: best-effort
            # PID kill. Covers the case where MCP restarts and finds
            # stale sessions.json entries from a previous run. The kill
            # is refused when the PID now belongs to a non-PPSSPP image
            # (OS PID reuse).
            with contextlib.suppress(Exception):
                await asyncio.to_thread(_kill_stale_pid_safe, sess_snapshot.pid)

        # Phase 3: brief lock to update sessions.json.
        async with self._lock:
            sessions = await _load_sessions_async()
            sessions.pop(session_id, None)
            await _save_sessions_async(sessions)

        return sess_snapshot.with_stopped()

    async def get_transport(self, session_id: str) -> WsTransport:
        """Return the session-level WsTransport for the given session_id.

        Read-only dict access (no lock needed — dict reads are atomic in
        CPython). Used by client_helper.session_client_with_transport to
        reuse the session-level transport.

        Raises:
            SessionNotFound: session_id not in _transports (either the
                session was loaded from disk, or session-level transport
                establishment failed in start_session). Callers (e.g.
                client_helper) fall back to per-call transport creation.
        """
        transport = self._transports.get(session_id)
        if transport is None:
            raise SessionNotFound(
                f"session transport not found: {session_id} "
                f"(session may be disk-loaded or transport establishment "
                f"failed)"
            )
        return transport

    async def get_observer(self, session_id: str) -> GameStateObserver:
        """Return the session-level GameStateObserver for the given session_id.

        Read-only dict access (no lock needed). Used by client_helper to
        retrieve the observer for PpssppDebugClient construction, which
        forwards it to SteppingManager for resume() broadcast
        confirmation.

        Raises:
            SessionNotFound: session_id not in _observers.
        """
        observer = self._observers.get(session_id)
        if observer is None:
            raise SessionNotFound(
                f"session observer not found: {session_id} "
                f"(session may be disk-loaded or observer establishment "
                f"failed)"
            )
        return observer

    async def get_session_state(self, session_id: str) -> Session:
        """Get a session's current state (pure read, no side effects).

        Returns a snapshot of the session without modifying `exec_count`,
        `last_active_at`, or any other field. Does not call `_save_sessions`.

        Tools that mutate session state (step / read_memory / write_memory /
        breakpoint / etc.) should call `touch_session(sid)` to update
        activity tracking and persist it.

        Raises:
            SessionNotFound: session_id not in active sessions.
            SessionExpired: session process is no longer alive.
        """
        sessions = await _load_sessions_async()
        sess = sessions.get(session_id)
        if sess is None:
            raise SessionNotFound(f"session not found: {session_id}")
        if sess.pid is not None and not proc.is_pid_alive(sess.pid):
            raise SessionExpired(f"session process {sess.pid} is no longer alive")
        # Report the *actual* transport connection state instead of the
        # ws_connected flag, which only updates when a tool call
        # enters/leaves session_client (it goes stale and even
        # oscillates between calls). Pure read: the live value is
        # applied to the returned snapshot only, never persisted here.
        transport = self._transports.get(session_id)
        if transport is not None:
            sess = sess.with_ws_connected(transport.is_connected())
        # Pure read: return the session as-is (no activity bump, no persist).
        return sess

    async def touch_session(self, session_id: str) -> Session:
        """Explicitly mark a session as active and persist the update.

        Updates `last_active_at` to now and bumps `exec_count` by 1, then
        calls `_save_sessions` to persist. Tools that mutate session state
        (step / read_memory / write_memory / breakpoint / etc.) should call
        this so the idle GC sees recent activity and doesn't reap the
        session.

        Raises:
            SessionNotFound: session_id not in active sessions.
        """
        async with self._lock:
            sessions = await _load_sessions_async()
            sess = sessions.get(session_id)
            if sess is None:
                raise SessionNotFound(f"session not found: {session_id}")
            touched = sess.with_updated_activity()
            # Fold the live WS state in here so client_helper no longer
            # needs per-call update_ws_connected(True/False) writes
            # (get_session_state already reports transport.is_connected();
            # per-call writes cost 4 extra sessions.json file ops per
            # tool call and oscillate the persisted flag under
            # concurrent calls).
            transport = self._transports.get(session_id)
            if transport is not None:
                touched = touched.with_ws_connected(transport.is_connected())
            sessions[session_id] = touched
            await _save_sessions_async(sessions)
            return touched

    async def update_ws_connected(self, session_id: str, connected: bool) -> None:
        """Update ws_connected flag and persist.

        Called by session_client_with_transport after a successful
        WS connect+handshake (connected=True) or on disconnect (False).
        The previous code never updated ws_connected after session creation,
        so it stayed False forever — making it a dead field in session_list.
        """
        async with self._lock:
            sessions = await _load_sessions_async()
            sess = sessions.get(session_id)
            if sess is None:
                return  # Session already stopped/removed — nothing to update.
            sessions[session_id] = sess.with_ws_connected(connected)
            await _save_sessions_async(sessions)

    async def list_sessions(self) -> list[Session]:
        """List all active sessions (pure read, no GC side effect).

        Returns sessions that are alive and not idle-expired. Does NOT
        perform idle GC — use `gc_idle_sessions()` for that.

        Async: uses _load_sessions_async() to avoid blocking the event
        loop on file I/O (sessions.json may be on a slow network mount).
        """
        sessions = await _load_sessions_async()
        alive: list[Session] = []
        for _sid, sess in sessions.items():
            if sess.pid is not None and not proc.is_pid_alive(sess.pid):
                continue
            if _idle_seconds(sess) > IDLE_GC_THRESHOLD_S:
                continue
            alive.append(sess)
        return alive

    async def gc_idle_sessions(self) -> list[str]:
        """Stop idle/expired sessions and return their session_ids.

        Three-phase locking protocol (mirrors stop_session) to avoid
        blocking other sessions during potentially slow launcher.stop:

        1. **Acquire lock briefly** to scan for expired sessions and pop
           their in-memory launchers. Release lock.
        2. **Outside the lock**, offload all launcher.stop / _force_kill_pid
           calls to threads. Other sessions can start/stop concurrently.
        3. **Re-acquire lock briefly** to remove stopped sessions from
           sessions.json and persist.

        Returns:
            List of session_ids that were stopped.
        """
        stopped_ids: list[str] = []

        # Phase 1: brief lock to scan + pop launchers.
        async with self._lock:
            sessions = await _load_sessions_async()
            expired: list[str] = []
            for sid, sess in sessions.items():
                if sess.pid is not None and not proc.is_pid_alive(sess.pid):
                    expired.append(sid)
                    continue
                if _idle_seconds(sess) > IDLE_GC_THRESHOLD_S:
                    expired.append(sid)
                    continue
            if not expired:
                return stopped_ids
            # Pop launchers/transports/observers while holding the lock
            # (dicts are protected). Transports/observers MUST be popped
            # here — a GC'd session would otherwise leak its WsTransport
            # (dead WS object + events queue + pending map) and
            # GameStateObserver for the remaining server lifetime.
            expired_launchers: dict[str, PpssppLauncher | None] = {}
            expired_transports: dict[str, Any] = {}
            expired_observers: dict[str, Any] = {}
            expired_pids: dict[str, int | None] = {}
            for sid in expired:
                expired_launchers[sid] = self._launchers.pop(sid, None)
                expired_transports[sid] = self._transports.pop(sid, None)
                expired_observers[sid] = self._observers.pop(sid, None)
                sess = sessions.get(sid)
                expired_pids[sid] = sess.pid if sess else None

        # Phase 2: outside the lock — stop processes concurrently.
        # Each launcher.stop / _force_kill_pid can take 5+ seconds for
        # a hung process; running them outside the lock prevents blocking
        # all other session operations.
        for sid in expired:
            # Close observer then transport first (same order as
            # stop_session phase 1.5) — best-effort, outside the lock.
            observer = expired_observers.get(sid)
            if observer is not None:
                with contextlib.suppress(Exception):
                    await observer.stop()
            transport = expired_transports.get(sid)
            if transport is not None:
                with contextlib.suppress(Exception):
                    await transport.close()
            launcher = expired_launchers[sid]
            if launcher is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(launcher.stop)
            else:
                pid = expired_pids[sid]
                if pid is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(_kill_stale_pid_safe, pid)
            stopped_ids.append(sid)

        # Phase 3: brief lock to update sessions.json.
        async with self._lock:
            sessions = await _load_sessions_async()
            for sid in stopped_ids:
                sessions.pop(sid, None)
            await _save_sessions_async(sessions)

        return stopped_ids

    async def idle_gc_loop(self) -> None:
        """Background asyncio task: periodically scan & GC idle sessions."""
        while True:
            try:
                await asyncio.sleep(IDLE_GC_INTERVAL_S)
                await self.gc_idle_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Don't let the GC task die on errors.
                log.warning("idle_gc error: %s", e)
                continue


# Module-level singleton.

_default_manager: SessionManager | None = None


def get_session_manager() -> SessionManager:
    """Return the module-level singleton SessionManager.

    Lazily constructs a SessionManager on first call. Subsequent calls
    return the same instance.
    """
    global _default_manager
    if _default_manager is None:
        _default_manager = SessionManager()
    return _default_manager


# Backward-compat module-level functions (delegate to singleton). Existing
# callers (tools/session.py, server.py, tools/introspect.py) keep working
# without modification. Note: start_session, stop_session, touch_session,
# and get_session_state are now async (get_session_state changed from sync
# to async to avoid blocking I/O on sessions.json — callers must update).
async def start_session(
    iso_path: str,
    *,
    resilient: bool = False,
    ready_timeout_s: float = 75.0,
    max_restarts: int = 2,
    probe_addr: int = DEFAULT_PROBE_ADDR,
) -> Session:
    return await get_session_manager().start_session(
        iso_path,
        resilient=resilient,
        ready_timeout_s=ready_timeout_s,
        max_restarts=max_restarts,
        probe_addr=probe_addr,
    )


async def stop_session(session_id: str) -> Session:
    return await get_session_manager().stop_session(session_id)


async def get_session_state(session_id: str) -> Session:
    return await get_session_manager().get_session_state(session_id)


async def touch_session(session_id: str) -> Session:
    return await get_session_manager().touch_session(session_id)


async def update_ws_connected(session_id: str, connected: bool) -> None:
    """Module-level wrapper for SessionManager.update_ws_connected."""
    return await get_session_manager().update_ws_connected(session_id, connected)


async def get_transport(session_id: str) -> WsTransport:
    """Module-level wrapper for SessionManager.get_transport.

    Used by client_helper.session_client_with_transport to retrieve the
    session-level transport.
    """
    return await get_session_manager().get_transport(session_id)


async def get_observer(session_id: str) -> GameStateObserver:
    """Module-level wrapper for SessionManager.get_observer.

    Used by client_helper.session_client_with_transport to retrieve the
    session-level observer for PpssppDebugClient construction.
    """
    return await get_session_manager().get_observer(session_id)


async def list_sessions() -> list[Session]:
    return await get_session_manager().list_sessions()


async def gc_idle_sessions() -> list[str]:
    return await get_session_manager().gc_idle_sessions()


def _check_port_conflict(
    sessions: dict[str, Session],
    new_session_id: str,
    new_port: int,
) -> None:
    """Raise PortConflict if new_port is already used by an active session.

    Only checks against sessions with a live PID (stopped sessions with
    pid=None are ignored — their port is free). This prevents a second
    session from silently sharing the first session's PPSSPP process
    when a fixed port is configured.
    """
    for sid, sess in sessions.items():
        if sid == new_session_id:
            continue
        if sess.pid is None:
            continue  # Stopped session — port is free.
        if not proc.is_pid_alive(sess.pid):
            continue  # Dead process — port is free.
        # Extract port from ws_url (format: ws://host:port/debugger).
        existing_port = _extract_port_from_ws_url(sess.ws_url)
        if existing_port is not None and existing_port == new_port:
            raise PortConflict(
                f"port {new_port} is already in use by active session "
                f"{sid} (pid={sess.pid}). Use a different port or stop "
                f"the existing session first.",
            )


def _extract_port_from_ws_url(ws_url: str) -> int | None:
    """Extract the port from a ws://host:port/path URL.

    Returns None if the URL is malformed or has no explicit port.
    Uses urllib.parse for correct IPv6 literal handling.
    """
    from urllib.parse import urlparse

    try:
        return urlparse(ws_url).port
    except (ValueError, AttributeError):
        return None


async def _probe_ws_connection(ws_url: str) -> bool:
    """Probe whether the WS endpoint accepts a real client.

    ``launcher.start()`` only waits for the TCP port to be listening
    (``_is_port_listening`` opens a raw socket and checks ``connect_ex``).
    A listening socket does NOT guarantee PPSSPP's WebSocket debugger is
    ready to accept a subprotocol handshake — on cold starts there is a
    short window where the port is bound but the WS handler rejects
    connections. Callers that query ``session(get)`` immediately after
    ``start_session`` previously saw ``ws_connected=False`` and had to
    retry on the first tool call to learn the real state.

    This probe performs the real handshake (``WsTransport.connect`` +
    ``send_version``) and returns ``True`` only when PPSSPP responds to
    the version event. Failures are logged at DEBUG and swallowed —
    ``ws_connected`` stays ``False`` and the caller falls back to the
    existing "first tool call discovers the connection" path. The probe
    MUST NOT raise: it is best-effort, run after the session is already
    persisted, so a transient PPSSPP cold-start delay should not break
    ``start_session``.

    The transport is always closed (best-effort) so no recv loop leaks.

    Args:
        ws_url: WebSocket URL (``ws://host:port/debugger``).

    Returns:
        True if the WS handshake + version event succeeded; False on any
        connection / handshake / version failure.
    """
    from urllib.parse import urlparse

    from ppsspp_dfx_mcp.core.transport import WsTransport

    # Reject non-string input up front. Python 3.14's urlparse no longer
    # raises on None/bytes — it coerces to ParseResultBytes with empty
    # fields, which would fall through to the default host/port and
    # attempt a real connection instead of being treated as malformed.
    if not isinstance(ws_url, str):
        log.warning("N-07 probe: malformed ws_url %r", ws_url)
        return False

    try:
        url = urlparse(ws_url)
        host = url.hostname or "127.0.0.1"
        port = url.port or 12345
    except (ValueError, AttributeError):
        log.warning("N-07 probe: malformed ws_url %r", ws_url)
        return False

    transport = WsTransport(host, port)
    try:
        await transport.connect()
        await transport.send_version()
    except Exception as e:
        # Cold-start window: PPSSPP's WS handler is not yet accepting
        # connections / subprotocol negotiation not ready. Swallow and
        # let the first tool call's session_client_with_transport retry.
        log.debug(
            "N-07 probe: WS not ready for %s (host=%s port=%s): %s",
            ws_url,
            host,
            port,
            e,
        )
        return False
    finally:
        # Best-effort close; do not mask the original result.
        with contextlib.suppress(Exception):
            await transport.close()
    return True


async def idle_gc_loop() -> None:
    await get_session_manager().idle_gc_loop()
