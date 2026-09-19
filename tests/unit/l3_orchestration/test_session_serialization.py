"""W1 fix tests: per-session tool-call serialization.

Real-PPSSPP evidence (review-r2 probes, review_r2_probes.json): two
CONCURRENT step_into() calls both returned success while PPSSPP itself
logged "Can't submit two steps in one host frame" — one step was rejected
yet both callers consumed a confirmation broadcast. The per-session lock
in SessionManager + client_helper makes the single-consumer contracts
(transport, observer queues, CPU stepping state) enforceable.

Covers:
- session_lock() get-or-create identity (same sid → same lock).
- stop_session pops the lock entry (no leak across session lifecycles).
- Two concurrent session_client_with_transport bodies on the same session
  are serialized (non-overlapping execution windows).
- A caller that cannot get the lock within SESSION_BUSY_TIMEOUT_S gets
  SessionBusy (not an unbounded silent queue).
- Fake mode is NOT serialized (each call owns a private FakeTransport).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from ppsspp_dfx_mcp.errors import SessionBusy
from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session import client_helper, session_manager
from ppsspp_dfx_mcp.session.session_manager import SessionManager


class _StubTransport:
    """Duck-typed transport — only is_connected() is touched off-body."""

    def __init__(self) -> None:
        self.closed = False

    def is_connected(self) -> bool:
        return not self.closed


class _StubObserver:
    def get_state(self) -> str:
        return "running"


def _fresh_manager(monkeypatch: pytest.MonkeyPatch) -> SessionManager:
    """Isolate the module-level singleton for this test."""
    mgr = SessionManager()
    monkeypatch.setattr(session_manager, "_default_manager", mgr)
    return mgr


async def _seed_session(mgr: SessionManager, sid: str) -> None:
    sess = Session(
        session_id=sid,
        iso_path="unused.iso",
        pid=None,
        ws_url="ws://127.0.0.1:1/debugger",
    )
    await session_manager._save_sessions_async({sid: sess})
    mgr._transports[sid] = _StubTransport()
    mgr._observers[sid] = _StubObserver()


@pytest.mark.asyncio
async def test_session_lock_same_instance_per_sid():
    mgr = SessionManager()
    lock_a1 = mgr.session_lock("s1")
    lock_a2 = mgr.session_lock("s1")
    lock_b = mgr.session_lock("s2")
    assert lock_a1 is lock_a2
    assert lock_a1 is not lock_b


@pytest.mark.asyncio
async def test_stop_session_pops_lock(monkeypatch: pytest.MonkeyPatch):
    """🟡2 (updated contract): the lock entry is popped in Phase 3 — after
    the process is gone — so in-flight holders keep a consistent lock and
    concurrent callers still serialize (on the OLD lock) until then."""
    mgr = _fresh_manager(monkeypatch)
    await _seed_session(mgr, "s1")
    lock = mgr.session_lock("s1")
    assert "s1" in mgr._session_locks
    await mgr.stop_session("s1")
    assert "s1" not in mgr._session_locks
    # The popped lock object is untouched (any holder still releases fine).
    assert not lock.locked()


@pytest.mark.asyncio
async def test_concurrent_session_clients_serialize(
    monkeypatch: pytest.MonkeyPatch,
):
    mgr = _fresh_manager(monkeypatch)
    await _seed_session(mgr, "s1")

    windows: list[tuple[float, float]] = []

    async def _call_body() -> None:
        async with client_helper.session_client_with_transport("s1"):
            enter = time.monotonic()
            await asyncio.sleep(0.05)
            windows.append((enter, time.monotonic()))

    await asyncio.gather(_call_body(), _call_body())

    assert len(windows) == 2
    early, late = sorted(windows)
    # Serialized: the second body can only enter after the first released.
    assert late[0] >= early[1], f"session bodies overlapped: {windows}"


@pytest.mark.asyncio
async def test_session_busy_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    mgr = _fresh_manager(monkeypatch)
    await _seed_session(mgr, "s1")
    monkeypatch.setattr(client_helper, "SESSION_BUSY_TIMEOUT_S", 0.05)

    lock = mgr.session_lock("s1")
    # The lock is same-task reentrant (D3): hold it in a DIFFERENT task so
    # the session_client call below genuinely contends.
    holder_ready = asyncio.Event()
    release_lock = asyncio.Event()

    async def hold_lock() -> None:
        await lock.acquire()
        holder_ready.set()
        await release_lock.wait()
        lock.release()

    holder = asyncio.create_task(hold_lock())
    try:
        await asyncio.wait_for(holder_ready.wait(), timeout=1.0)
        with pytest.raises(SessionBusy):
            async with client_helper.session_client_with_transport("s1"):
                pass  # pragma: no cover — must not be reached
    finally:
        release_lock.set()
        await asyncio.gather(holder, return_exceptions=True)


@pytest.mark.asyncio
async def test_fake_mode_not_serialized(monkeypatch: pytest.MonkeyPatch):
    """Fake mode builds a private FakeTransport per call — no lock taken."""
    mgr = _fresh_manager(monkeypatch)
    monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "fake")
    # get_session_state runs before the mode dispatch — the session record
    # must exist even in fake mode.
    await _seed_session(mgr, "s-fake")

    class _FakeTransport:
        pass

    def _fake_build() -> _FakeTransport:
        return _FakeTransport()

    monkeypatch.setattr(client_helper, "_build_fake_transport_for_session", _fake_build)

    # Simulate a held lock for this sid — fake mode must ignore it.
    lock = mgr.session_lock("s-fake")
    await lock.acquire()
    try:

        async def _body() -> str:
            async with client_helper.session_client_with_transport("s-fake") as (
                _client,
                transport,
            ):
                assert isinstance(transport, _FakeTransport)
                return "ok"

        results = await asyncio.wait_for(asyncio.gather(_body(), _body()), timeout=2.0)
        assert results == ["ok", "ok"]
    finally:
        lock.release()
