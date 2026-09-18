"""Session-scoped DebugClient / CaptureService helpers.

`session_client(session_id)` yields a connected `PpssppDebugClient`;
`session_capture(session_id)` yields a `(PpssppDebugClient,
CaptureService)` tuple. Sync utilities: `validate_session_alive`
(liveness check without WS) and `read_game_mode_addr` (pure config
read from addresses.yaml).

No client caching — each tool call opens a fresh connection (~50ms).
`session_client_with_transport` reuses the session-level `WsTransport`
from `SessionManager.start_session`; the client stays per-call
(stateless), with per-call fallback when no session transport exists.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ppsspp_dfx_mcp.config import addresses as _addresses
from ppsspp_dfx_mcp.config import fixture_dir, test_mode
from ppsspp_dfx_mcp.core.transport import WsTransport
from ppsspp_dfx_mcp.errors import (
    ArgsInvalid,
    SessionAmbiguous,
    SessionBusy,
    SessionNotFound,
    reset_error_context,
    set_error_context,
)
from ppsspp_dfx_mcp.service.capture import CaptureService
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient
from ppsspp_dfx_mcp.session import session_manager

logger = logging.getLogger(__name__)

# How long a tool call waits for the per-session lock
# before failing with SessionBusy. Long enough to absorb legitimate burst
# concurrency (several quick reads), short enough that a caller queued
# behind a long wait_frames/batch_step gets a fast actionable error.
SESSION_BUSY_TIMEOUT_S = 5.0

# Per-session FakeTransport cache — the byte-level write overlay must
# survive across tool calls within a session.
_FAKE_TRANSPORTS: dict[str, Any] = {}


async def resolve_session_id(session_id: str | None) -> str:
    """Resolve an omitted session_id against the active-session table.

    Resolution policy: an explicit session_id passes through
    untouched; when omitted, exactly ONE active session resolves silently
    (the common single-game case), while 0 sessions raise a hint to start
    one and 2+ sessions raise ``SessionAmbiguous`` listing every id.
    """
    if session_id:
        return session_id
    sessions = await session_manager.list_sessions()
    if not sessions:
        raise ArgsInvalid(
            "session_id is required — no active session; start one with "
            'ppsspp_session(action="start", iso_path=...)'
        )
    if len(sessions) > 1:
        ids = ", ".join(s.session_id for s in sessions)
        raise SessionAmbiguous(
            f"session_id is ambiguous — {len(sessions)} active sessions "
            f"({ids}); pass session_id explicitly or stop the extras "
            '(ppsspp_session(action="list") / ppsspp_session(action="stop"))'
        )
    return sessions[0].session_id


def _build_fake_transport_for_session() -> Any:
    """Build a FakeTransport pre-loaded with recorded fixtures.

    Used by `session_client_with_transport` when PPSSPP_DFX_TEST_MODE=fake.
    The FakeTransport is loaded via `contract_recorder.fixture_loader.load_all`
    so tool calls receive realistic PPSSPP responses without a live PPSSPP.

    The FakeTransport class lives in `tests/fake_transport/` (test tree,
    not src tree). Importing it requires `tests/` on sys.path, which is
    set up by `tests/conftest.py` for pytest runs. For the MCP Inspector
    test fixture, the conftest that launches the server subprocess also
    injects `tests/` into PYTHONPATH.

    Returns:
        FakeTransport instance with recorded responses injected.

    Raises:
        RuntimeError: PPSSPP_DFX_FIXTURE_DIR not set or points at a
            non-existent directory.
    """
    # Lazy imports: FakeTransport + load_all live in the test tree, so
    # importing them at module load would fail in production (no tests/
    # on sys.path). Defer to call time so the production code path
    # (test_mode == "") never triggers these imports.
    from contract_recorder.fixture_loader import load_all  # type: ignore[import-not-found]
    from fake_transport import FakeTransport  # type: ignore[import-not-found]

    fdir = fixture_dir()
    if fdir is None:
        raise RuntimeError(
            "PPSSPP_DFX_TEST_MODE=fake requires PPSSPP_DFX_FIXTURE_DIR "
            "to point at a recorded fixtures directory (run "
            "`python -m ppsspp_dfx_mcp.scripts.record_fixtures` to produce one)"
        )
    if not Path(fdir).is_dir():
        raise RuntimeError(f"PPSSPP_DFX_FIXTURE_DIR points at non-existent dir: {fdir}")

    fake = FakeTransport()
    fake.set_state({"stepping": False})

    # Wire stepping faf handlers so DebugClient.with_stepping works
    # without manual state setup.
    def _set_stepping_true(t: Any, **params: Any) -> None:
        cur = t.state
        t.set_state({**cur, "stepping": True})

    def _set_stepping_false(t: Any, **params: Any) -> None:
        cur = t.state
        t.set_state({**cur, "stepping": False})

    fake.set_faf_handler("cpu.stepping", _set_stepping_true)
    fake.set_faf_handler("cpu.resume", _set_stepping_false)

    load_all(Path(fdir), fake)
    return fake


@asynccontextmanager
async def session_client_with_transport(
    session_id: str,
) -> AsyncIterator[tuple[PpssppDebugClient, Any]]:
    """Yield a (PpssppDebugClient, WsTransport) tuple for the session.

    In production mode: reuses the session-level ``WsTransport``
    established by ``SessionManager.start_session``. The transport is
    NOT closed on exit; ``SessionManager.stop_session`` owns the
    transport lifecycle.
    ``PpssppDebugClient`` is per-call constructed (stateless) with the
    session's ``pid`` + ``GameStateObserver`` forwarded to
    ``SteppingManager`` for PID pre-check + resume broadcast
    confirmation. Both the client and the backing transport are yielded
    so callers that need direct transport access (e.g. CaptureService
    for gpu.buffer.screenshot) can pass the transport explicitly
    instead of reaching into ``_client._transport``.

    Fallback: if the session-level transport is unavailable
    (``SessionNotFound`` — e.g. transport establishment failed in
    ``start_session``, or session loaded from disk with no live
    transport), falls back to per-call ``WsTransport`` creation (legacy
    path). This keeps unit tests (which use ``_StubLauncher`` without a
    real PPSSPP) working without modification.

    In fake test mode (PPSSPP_DFX_TEST_MODE=fake): substitutes a
    FakeTransport pre-loaded with recorded fixtures for the real
    WsTransport. No network I/O is performed. The transport's `events`
    queue is an asyncio.Queue, matching the WsTransport protocol.

    Calls `touch_session(session_id)` on entry to mark the session as
    active (bumps exec_count + last_active_at + persists) so the idle GC
    sees recent activity.

    Raises:
        SessionNotFound: session_id not in active sessions.
        SessionExpired: session process is no longer alive (skipped in
            fake mode since pid is None).
        SessionBusy: another tool call holds the per-session
            lock for longer than SESSION_BUSY_TIMEOUT_S (production and
            fallback modes only; fake mode is not serialized).
        ConnectionRefusedError: PPSSPP not started or port not listening
            (not raised in fake mode; only in per-call fallback path).
        RuntimeError: WebSocket subprotocol negotiation failed (not
            raised in fake mode; only in per-call fallback path).
    """
    sess = await session_manager.get_session_state(session_id)
    # Mark the session as actively used (bumps exec_count + persists).
    # Done after get_session_state so SessionNotFound/SessionExpired
    # propagate before we write.
    await session_manager.touch_session(session_id)

    # ── Fake test mode: FakeTransport + recorded fixtures ──
    # Fake mode is NOT serialized — calls share ONE transport
    # per session: the write overlay must persist
    # across tool calls or a successful write is silently undone by the
    # next call's fixture-backed read. Concurrency on the shared
    # overlay is acceptable for a test double (dict ops are GIL-atomic).
    if test_mode() == "fake":
        fake = _FAKE_TRANSPORTS.get(session_id)
        if fake is None:
            fake = _build_fake_transport_for_session()
            _FAKE_TRANSPORTS[session_id] = fake
        # Set error context per-tool-call scope so multi-session
        # concurrent calls don't pollute each other's resolvers. Fake
        # mode has no real PID/observer — resolvers stay None
        # (conservative translation path).
        err_token = set_error_context(None, None)
        try:
            yield PpssppDebugClient(fake), fake
        finally:
            reset_error_context(err_token)
            # No per-call ws_connected persistence — touch_session folds
            # the live transport state in, and fake mode has no real
            # transport anyway. FakeTransport has no close() —
            # best-effort no-op.
        return

    # ── Per-session serialization ──
    # The MCP SDK dispatches tool calls concurrently (each request runs in
    # its own task), but the session-level WsTransport, the
    # GameStateObserver per-event queues, and the global CPU stepping state
    # are all single-consumer. Real-PPSSPP evidence:
    # two concurrent step_into() calls both reported success while PPSSPP
    # itself logged "Can't submit two steps in one host frame" — one step
    # was rejected yet BOTH callers got a confirmation broadcast. Both the
    # session-reuse path AND the per-call fallback path share the same
    # PPSSPP CPU, so both hold the lock for the whole body.
    #
    # Busy policy: wait up to SESSION_BUSY_TIMEOUT_S for a legitimate short
    # overlap, then fail with SessionBusy instead of silently queueing
    # behind a long wait_frames/batch_step call.
    session_lock = session_manager.get_session_manager().session_lock(session_id)
    try:
        await asyncio.wait_for(session_lock.acquire(), timeout=SESSION_BUSY_TIMEOUT_S)
    except TimeoutError as e:
        # If the lock is held by a detached background batch, point
        # the caller at the status/cancel tools instead of a blind retry.
        from ppsspp_dfx_mcp.core.batch_jobs import get_registry

        bg_job = get_registry().running_job_for_session(session_id)
        hint = ""
        if bg_job is not None:
            hint = (
                f" A background batch ({bg_job.batch_id}) is currently "
                f"executing on this session — poll ppsspp_batch_status("
                f"batch_id='{bg_job.batch_id}') or cancel it via "
                f"ppsspp_batch_cancel."
            )
        raise SessionBusy(
            f"session {session_id} is busy with another tool call "
            f"(waited {SESSION_BUSY_TIMEOUT_S}s). PPSSPP debugging sessions "
            f"serialize one tool call at a time — retry shortly." + hint
        ) from e
    try:
        async with _session_transport_context(sess, session_id) as (
            client,
            transport,
        ):
            yield client, transport
    finally:
        session_lock.release()


@asynccontextmanager
async def _session_transport_context(
    sess: Any, session_id: str
) -> AsyncIterator[tuple[PpssppDebugClient, Any]]:
    """Yield (client, transport) for a validated session.

    Extracted from session_client_with_transport so the per-session lock
    wraps exactly this region. Reuses the session-level transport when
    available; falls back to a per-call transport otherwise. Callers own
    the lock; this helper owns nothing.
    """
    # Try to reuse the session-level WsTransport + GameStateObserver
    # established by SessionManager.start_session. If unavailable
    # (SessionNotFound — transport establishment failed or session
    # loaded from disk), fall back to per-call WsTransport creation
    # (graceful degradation).
    session_transport = None
    session_observer = None
    try:
        session_transport = await session_manager.get_transport(session_id)
        session_observer = await session_manager.get_observer(session_id)
    except SessionNotFound:
        # Session-level transport not available — fall back to per-call.
        session_transport = None

    if session_transport is not None:
        # Reuse the session-level transport. Do NOT close on exit —
        # SessionManager.stop_session owns the transport lifecycle.
        # PpssppDebugClient is per-call constructed (stateless) with
        # pid + observer forwarded to SteppingManager for PID pre-check
        # + resume broadcast confirm.
        #
        # Set error context per-tool-call scope (PID + game-state
        # resolvers) so multi-session concurrent calls don't pollute
        # each other's resolvers.
        err_token = set_error_context(
            pid_resolver=(lambda s=sess: s.pid) if sess.pid is not None else None,
            game_state_resolver=session_observer.get_state
            if session_observer is not None
            else None,
        )
        try:
            yield (
                PpssppDebugClient(
                    session_transport,
                    pid=sess.pid,
                    game_state_observer=session_observer,
                ),
                session_transport,
            )
        finally:
            reset_error_context(err_token)
            # No per-call ws_connected persistence — the transport is
            # session-level and its live state is folded into
            # touch_session / get_session_state instead. This removes 4
            # sessions.json file ops per tool call and the True/False
            # oscillation under concurrent calls.
        return

    # ── Fallback: per-call WsTransport (legacy path) ──
    # Used when session-level transport is unavailable (SessionNotFound).
    # Builds a fresh WsTransport from the session's ws_url, connects it,
    # runs the version handshake, and wraps it in a PpssppDebugClient.
    # The transport IS closed on exit (per-call lifecycle).
    #
    # Forward pid to PpssppDebugClient so pause() can do PID pre-check
    # even in fallback. observer is None in fallback (no session-level
    # observer) — acceptable, resume() falls back to wait_for_state
    # polling.
    url = urlparse(sess.ws_url)
    host = url.hostname or "127.0.0.1"
    port = url.port or 12345

    transport = WsTransport(host, port)
    # Set error context per-tool-call scope.
    err_token = set_error_context(
        pid_resolver=(lambda s=sess: s.pid) if sess.pid is not None else None,
        game_state_resolver=None,
    )
    try:
        await transport.connect()
        await transport.send_version()
        yield PpssppDebugClient(transport, pid=sess.pid), transport
    finally:
        reset_error_context(err_token)
        # No per-call ws_connected persistence — touch_session folds in
        # the live transport state instead. The per-call transport IS
        # closed here — this fallback path owns its lifecycle.
        # Best-effort close; do not mask the original exception.
        with suppress(Exception):
            await transport.close()


@asynccontextmanager
async def session_client(session_id: str) -> AsyncIterator[PpssppDebugClient]:
    """Yield a connected PpssppDebugClient for the given session_id.

    Thin wrapper around `session_client_with_transport` that yields only
    the client (for tools that don't need direct transport access).
    The transport is still created and closed internally — callers just
    don't see it.

    Raises:
        SessionNotFound: session_id not in active sessions.
        SessionExpired: session process is no longer alive.
        ConnectionRefusedError: PPSSPP not started or port not listening.
        RuntimeError: WebSocket subprotocol negotiation failed.
    """
    async with session_client_with_transport(session_id) as (client, _transport):
        yield client


@asynccontextmanager
async def session_capture(
    session_id: str,
) -> AsyncIterator[tuple[PpssppDebugClient, CaptureService]]:
    """Yield a (PpssppDebugClient, CaptureService) tuple for the session.

    Convenience helper for tools that need both the debug client (for
    stepping / read_bytes) and the capture service (for screenshot /
    dump_texture). Internally uses `session_client_with_transport` so
    the transport is explicitly injected into CaptureService (no
    `_client._transport` encapsulation violation).

    Raises:
        SessionNotFound: session_id not in active sessions.
        SessionExpired: session process is no longer alive.
        ConnectionRefusedError: PPSSPP not started or port not listening.
        RuntimeError: WebSocket subprotocol negotiation failed.
    """
    async with session_client_with_transport(session_id) as (client, transport):
        yield client, CaptureService(client, transport=transport)


async def validate_session_alive(session_id: str) -> None:
    """Async validate that session_id is alive and mark it as active.

    Use this when a tool only needs to verify session liveness without
    opening a WebSocket connection (e.g. `ppsspp_wait_frames` does a
    wall-clock sleep and only re-validates liveness between chunks).

    Calls `touch_session` so the idle GC sees recent activity and
    doesn't reap the session during long operations.

    Raises:
        SessionNotFound: session_id not in active sessions.
        SessionExpired: session process is no longer alive.
    """
    await session_manager.get_session_state(session_id)
    await session_manager.touch_session(session_id)


def read_game_mode_addr() -> int:
    """Read the game_mode_addr from addresses.yaml (sync helper).

    Returns 0 if the address is not configured. Used by smoke_test.

    Pure config-reading utility (not a WS operation), so it lives in
    client_helper alongside other sync session/config helpers.
    """
    addrs: dict[str, Any] = _addresses()
    raw = addrs.get("game_mode_addr", 0)
    if isinstance(raw, str):
        try:
            return int(raw, 16) if raw.startswith("0x") else int(raw)
        except ValueError:
            return 0
    return int(raw) if isinstance(raw, int) else 0
