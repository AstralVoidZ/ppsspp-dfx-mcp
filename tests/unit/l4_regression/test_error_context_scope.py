"""L4 regression: W1 (code review v3) — error-context scope vs. exception path.

``session_client_with_transport`` injects the PID / game-state resolvers
that ``to_tool_error`` reads to classify a timeout (suspected CPU freeze
vs. plain WS timeout). The resolvers used to be reset in the generator's
``finally``, i.e. BEFORE the exception reached the tool layer's
``except`` block — so ``_resolve_pid_alive()`` / ``_resolve_game_state()``
were always None on the real call boundary and the documented
``CPU_FREEZE_SUSPECTED`` timeout diagnosis never fired in production.
``tests/unit/l4_regression/test_freeze_misjudgment_fix.py`` masked this
because it sets the context and calls ``to_tool_error`` in the SAME
scope (no context manager in between).

Contract:
- Exception path: the resolvers stay readable after the ``async with``
  unwinds, so the tool layer's ``to_tool_error(e)`` still sees them.
- Normal exit: the resolvers ARE reset (no leak into later calls).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fake_transport import FakeTransport

import ppsspp_dfx_mcp.core.proc as proc_mod
import ppsspp_dfx_mcp.session.session_manager as sm_mod
from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver
from ppsspp_dfx_mcp.errors import (
    ToolError,
    _resolve_game_state,
    _resolve_pid_alive,
    set_error_context,
)
from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session.client_helper import session_client_with_transport
from ppsspp_dfx_mcp.tools._common import translate_tool_errors

_PID = 4242
_SID = "s-errctx"


@pytest.fixture(autouse=True)
def _clean_error_context():
    """Never leak resolvers between tests (the fix deliberately leaves the
    context set while an exception unwinds)."""
    set_error_context(None, None)
    yield
    set_error_context(None, None)


@pytest.fixture(autouse=True)
def _fresh_session_singleton():
    """``session_client_with_transport`` uses the module-level singleton."""
    sm_mod._default_manager = None
    yield
    sm_mod._default_manager = None


def _install_session(session_id: str, pid: int, state: str = "running") -> FakeTransport:
    """Register a live PID session with a session-level transport/observer.

    ``state`` is pinned on the observer so a test can tell two scopes
    apart (the generator binds ``observer.get_state`` at entry).
    """
    manager = sm_mod.get_session_manager()
    now = datetime.now(UTC)
    sessions = sm_mod._load_sessions()
    sessions[session_id] = Session(
        session_id=session_id,
        iso_path="/tmp/fake.iso",
        pid=pid,
        ws_url="ws://127.0.0.1:12345/debugger",
        created_at=now,
        last_active_at=now,
        exec_count=0,
        ws_connected=False,
    )
    sm_mod._save_sessions(sessions)

    transport = FakeTransport()
    transport.set_state({"stepping": False})
    observer = GameStateObserver(transport)
    observer.get_state = lambda: state  # type: ignore[method-assign]
    manager._transports[session_id] = transport
    manager._observers[session_id] = observer
    return transport


@pytest.fixture
def live_session_transport(monkeypatch: pytest.MonkeyPatch) -> FakeTransport:
    """Production path: session with a PID, a session-level transport and
    observer, and ``proc.is_pid_alive`` pinned to True."""
    monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "")
    monkeypatch.setattr(proc_mod, "is_pid_alive", lambda _pid: True)
    return _install_session(_SID, _PID)


async def test_resolvers_readable_after_exception_unwind(live_session_transport):
    """W1: a raising body must NOT blank the resolvers before the tool
    layer translates the exception."""
    with pytest.raises(TimeoutError):
        async with session_client_with_transport(_SID):
            assert _resolve_pid_alive() is True
            raise TimeoutError("gpu.stats.get timed out")

    # This assertion runs where the tool layer runs ``to_tool_error(e)``:
    # after ``__aexit__`` has unwound the generator.
    assert _resolve_pid_alive() is True, (
        "the PID resolver must survive the exception path — otherwise "
        "_translate_timeout_error can never distinguish a PID-alive freeze "
        "from a plain WS timeout (CPU_FREEZE_SUSPECTED was dead code)."
    )


async def test_resolvers_reset_after_normal_exit(live_session_transport):
    """W1: normal exit still resets the contextvars (no leak)."""
    async with session_client_with_transport(_SID):
        assert _resolve_pid_alive() is True

    assert _resolve_pid_alive() is None
    assert _resolve_game_state() is None


async def test_concurrent_sessions_keep_their_own_resolvers(monkeypatch: pytest.MonkeyPatch):
    """W1: the injected scope is per-asyncio-task — two concurrent tool
    calls on different sessions must each resolve their OWN PID /
    game-state (no cross-session pollution, which is the reason
    contextvars were chosen over module-level state)."""
    monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "")
    monkeypatch.setattr(proc_mod, "is_pid_alive", lambda _pid: True)
    _install_session(_SID, _PID, state="running")
    _install_session("s-other", 7777, state="paused")

    seen: dict[str, tuple[bool | None, str | None]] = {}

    async def _probe(session_id: str) -> None:
        async with session_client_with_transport(session_id):
            # Yield control so the sibling task sets its own scope too.
            await asyncio.sleep(0.01)
            seen[session_id] = (_resolve_pid_alive(), _resolve_game_state())

    await asyncio.gather(_probe(_SID), _probe("s-other"))

    assert seen == {_SID: (True, "running"), "s-other": (True, "paused")}


async def test_timeout_on_real_boundary_yields_cpu_freeze(
    live_session_transport, monkeypatch: pytest.MonkeyPatch
):
    """W1 end-to-end: a WS call timing out inside the context manager must
    surface as ``[CPU_FREEZE_SUSPECTED]`` (PID alive + game running), not
    the conservative ``[WS_TIMEOUT]``."""
    observer = sm_mod.get_session_manager()._observers[_SID]
    monkeypatch.setattr(observer, "get_state", lambda: "running")

    async def _timeout_call(*_args: object, **_kwargs: object) -> None:
        raise TimeoutError("gpu.stats.get timed out")

    live_session_transport.call = _timeout_call  # type: ignore[method-assign]

    @translate_tool_errors
    async def _tool_body() -> None:
        async with session_client_with_transport(_SID) as (_client, transport):
            await transport.call("gpu.stats.get")

    with pytest.raises(ToolError) as exc_info:
        await _tool_body()

    assert exc_info.value.code == "CPU_FREEZE_SUSPECTED", (
        "a timeout raised on the real call boundary must render as "
        f"[CPU_FREEZE_SUSPECTED]; got {str(exc_info.value)!r}"
    )
