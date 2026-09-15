"""W2 fix tests: stale cpu.resume broadcasts are drained before resume.

Real-PPSSPP evidence (review-r2 probes): a cpu.resume broadcast injected
into the observer queue was consumed by the NEXT SteppingManager.resume()
in 0ms (returning {} via broadcast-confirm) instead of waiting for the
resume command's own broadcast — the confirmation was bypassed. The fix
drains the cpu.resume queue right before issuing the new command.
"""

from __future__ import annotations

import asyncio

import pytest

from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver
from ppsspp_dfx_mcp.core.stepping import SteppingManager


class _StubTransport:
    """Records fire_and_forget calls; call() is unused by resume()."""

    def __init__(self) -> None:
        self.faf: list[str] = []

    async def call(self, event: str, timeout: float = 5.0, **params):
        return {"stepping": False}

    async def fire_and_forget(self, event: str, **params) -> None:
        self.faf.append(event)

    async def wait_for_state(self, predicate, timeout_ms=3000, interval_ms=50):
        return {"stepping": False}


@pytest.mark.asyncio
async def test_drain_resume_drops_queued_broadcasts():
    observer = GameStateObserver.__new__(GameStateObserver)  # no start()
    observer._queues = {"cpu.resume": asyncio.Queue()}
    q = observer._queues["cpu.resume"]
    q.put_nowait({"event": "cpu.resume", "stale": 1})
    q.put_nowait({"event": "cpu.resume", "stale": 2})
    observer.drain_resume()
    assert q.qsize() == 0
    # Idempotent on an empty queue.
    observer.drain_resume()


@pytest.mark.asyncio
async def test_resume_drains_stale_broadcast_before_firing():
    observer = GameStateObserver.__new__(GameStateObserver)
    observer._queues = {"cpu.resume": asyncio.Queue()}
    observer._queues["cpu.resume"].put_nowait({"event": "cpu.resume", "stale": True})

    transport = _StubTransport()
    mgr = SteppingManager(transport, game_state_observer=observer)

    # wait_for_resume would consume the stale entry — stub the observer's
    # wait so the drain ordering is what we assert on (resume() must drain
    # BEFORE fire_and_forget and the wait must see an empty queue).
    async def _fake_wait(timeout_ms: int = 3000) -> bool:
        # If drain ran, the stale entry is gone; simulate the real
        # broadcast NOT arriving → fallback poll path.
        assert observer._queues["cpu.resume"].qsize() == 0, (
            "resume() must drain stale cpu.resume broadcasts before waiting"
        )
        return False

    monkey_wait = _fake_wait
    observer.wait_for_resume = monkey_wait  # type: ignore[method-assign]

    result = await mgr.resume()
    # Broadcast path returned False → polling fallback ran.
    assert result == {"stepping": False}
    assert transport.faf == ["cpu.resume"]


@pytest.mark.asyncio
async def test_resume_without_observer_still_polls():
    """Legacy path (no observer): resume must not regress."""
    transport = _StubTransport()
    mgr = SteppingManager(transport, game_state_observer=None)
    result = await mgr.resume()
    assert result == {"stepping": False}
    assert transport.faf == ["cpu.resume"]
