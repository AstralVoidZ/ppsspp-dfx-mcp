"""S1 regression lock: the MCP-side condition evaluator must be WIRED.

Review v3 【严重】S1: ``core/cond_filter.py`` (the MCP-side condition
evaluator) and the ``condition/condition_filtered/filtered_hits/storm_break``
response fields were declared, and the CHANGELOG claimed the behavior — but
the module had **zero production call sites**:

- ``ppsspp_breakpoint`` still shipped the ``condition`` to PPSSPP (which
  silently ignores register conditions in IR mode → conditional breakpoints
  fired unconditionally), and never registered anything in ``cond_filter``;
- ``wait_breakpoint`` never evaluated a filter, never auto-resumed a falsy
  hit, and had no storm breaker.

These tests encode the required behavior (先红后绿):

(a) a conditional breakpoint registers a filter AND sends condition=None;
(b) a falsy hit (``cpu.evaluate`` → 0) is filtered: not counted as a hit,
    ``filtered`` +1, CPU resumed;
(c) a truthy hit returns normally with ``condition`` + ``condition_filtered``;
(d) ≥10 fast hits auto-remove the breakpoint (``storm_break=true``) and drop
    the filter;
(e) ``stop_session`` drops the session's filters.

Harness notes: pure stubs (no FakeTransport) so the wire params, evaluator
results, and resume calls are all observable.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import ppsspp_dfx_mcp.tools.breakpoint as bp_mod
import ppsspp_dfx_mcp.tools.workflows as wf
from ppsspp_dfx_mcp.core import cond_filter
from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver
from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session import session_manager as sm

SESSION = "s1"
ADDR = 0x08804000
COND = "s1==0x711"


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    """No filter leakage between tests (module-level dict)."""
    cond_filter._filters.clear()
    yield
    cond_filter._filters.clear()


# ── stubs ────────────────────────────────────────────────────────────────


class _StubClient:
    """PPSSPP debug-client double: records wire params + evaluator script."""

    def __init__(self) -> None:
        self.stepping = False
        self.cpu_adds: list[dict] = []
        self.cpu_updates: list[dict] = []
        self.cpu_removes: list[int] = []
        self.mem_adds: list[dict] = []
        self.mem_removes: list[tuple[int, int]] = []
        self.resume_calls = 0
        self.evaluate_calls: list[str] = []
        # evaluate() script: returns these in order, holding the last.
        self.evaluate_values: list[int] = [0]

    async def cpu_bp_add(self, address, enabled=True, condition=None, **kw):
        self.cpu_adds.append({"address": address, "enabled": enabled, "condition": condition})
        return {}

    async def cpu_bp_update(self, address, enabled=None, log=None, condition=None, **kw):
        self.cpu_updates.append({"address": address, "condition": condition})
        return {}

    async def cpu_bp_remove(self, address):
        self.cpu_removes.append(address)
        return {}

    async def cpu_bp_list(self):
        entries = [{"address": a["address"], "enabled": a["enabled"]} for a in self.cpu_adds]
        return {"breakpoints": entries}

    async def mem_bp_add(
        self, address, size=4, read=True, write=True, enabled=True, log=False, **kw
    ):
        self.mem_adds.append({"address": address, "size": size})
        return {}

    async def mem_bp_list(self):
        return {"breakpoints": []}

    async def mem_bp_remove(self, address, size):
        self.mem_removes.append((address, size))
        return {}

    async def evaluate(self, expression, thread=None):
        self.evaluate_calls.append(expression)
        idx = min(len(self.evaluate_calls) - 1, len(self.evaluate_values) - 1)
        return {"value": self.evaluate_values[idx]}

    async def resume(self):
        self.resume_calls += 1
        self.stepping = False
        return {}

    async def safe_get_pc(self):
        return 0x08804008, "high"


class _ObserverTransport:
    """Observer input: the dispatcher consumes ``.events``."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[dict] = asyncio.Queue()

    async def call(self, event: str, **params):
        return {}


class _StubTransport:
    def __init__(self, client: _StubClient) -> None:
        self._client = client

    async def call(self, event: str, **params):
        assert event == "cpu.status"
        return {"stepping": self._client.stepping}


def _patch_breakpoint(monkeypatch, client: _StubClient) -> None:
    @asynccontextmanager
    async def fake_sc(session_id: str):
        yield client

    monkeypatch.setattr(bp_mod, "session_client", fake_sc)


def _patch_workflows(monkeypatch, client: _StubClient, observer: GameStateObserver) -> None:
    @asynccontextmanager
    async def fake_swt(session_id: str):
        yield client, _StubTransport(client)

    @asynccontextmanager
    async def fake_sc(session_id: str):
        yield client

    async def fake_alive(session_id: str) -> None:
        return None

    async def fake_get_observer(session_id: str):
        return observer

    monkeypatch.setattr(wf, "session_client_with_transport", fake_swt)
    monkeypatch.setattr(wf, "session_client", fake_sc)
    monkeypatch.setattr(wf, "validate_session_alive", fake_alive)
    monkeypatch.setattr(wf.session_manager, "get_observer", fake_get_observer)


def _push_hit(transport: _ObserverTransport, address: int = ADDR) -> None:
    transport.events.put_nowait(
        {
            "event": "cpu.stepping",
            "pc": 0x08804008,
            "relatedAddress": address,
            "reason": "breakpoint",
            "ticks": 1234.5,
        }
    )


# ── (a) set registers a filter + arms unconditionally ───────────────────


@pytest.mark.asyncio
async def test_set_conditional_registers_filter_and_omits_wire_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubClient()
    _patch_breakpoint(monkeypatch, client)

    await bp_mod.breakpoint(
        session_id=SESSION,
        action="set",
        address=hex(ADDR),
        condition=COND,
    )

    # The filter registry owns the condition...
    entry = cond_filter.get(SESSION, ADDR)
    assert entry is not None and entry["condition"] == COND
    # ...and PPSSPP receives NO condition (IR mode would silently ignore it).
    assert client.cpu_adds == [{"address": ADDR, "enabled": True, "condition": None}]


@pytest.mark.asyncio
async def test_mem_set_conditional_registers_filter_and_omits_wire_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _StubClient()
    _patch_breakpoint(monkeypatch, client)

    await bp_mod.breakpoint(
        session_id=SESSION,
        action="mem_set",
        address=hex(ADDR),
        condition=COND,
    )

    assert cond_filter.get(SESSION, ADDR) is not None
    assert client.mem_adds and client.mem_adds[0]["address"] == ADDR


@pytest.mark.asyncio
async def test_update_clears_condition_only_on_explicit_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """condition="" drops; condition=None (don't change) keeps the filter."""
    client = _StubClient()
    _patch_breakpoint(monkeypatch, client)
    cond_filter.register(SESSION, ADDR, COND)

    # None → "don't change" → registry untouched.
    await bp_mod.breakpoint(session_id=SESSION, action="update", address=hex(ADDR))
    assert cond_filter.get(SESSION, ADDR) is not None

    # "" → explicit clear → dropped, and still no wire condition.
    await bp_mod.breakpoint(session_id=SESSION, action="update", address=hex(ADDR), condition="")
    assert cond_filter.get(SESSION, ADDR) is None
    assert [u["condition"] for u in client.cpu_updates] == [None, None]


@pytest.mark.asyncio
async def test_remove_drops_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _StubClient()
    _patch_breakpoint(monkeypatch, client)
    # Pre-arm so remove's existence pre-check passes.
    client.cpu_adds.append({"address": ADDR, "enabled": True})
    cond_filter.register(SESSION, ADDR, COND)

    await bp_mod.breakpoint(session_id=SESSION, action="remove", address=hex(ADDR))

    assert client.cpu_removes == [ADDR]
    assert cond_filter.get(SESSION, ADDR) is None


# ── (b)/(c) wait-path evaluation ────────────────────────────────────────


@pytest.mark.asyncio
async def test_false_hit_is_filtered_and_resumed_then_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A falsy hit (evaluate → 0) is not a hit: filtered +1, CPU resumed."""
    transport = _ObserverTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    client = _StubClient()
    client.evaluate_values = [0]
    _patch_workflows(monkeypatch, client, observer)
    cond_filter.register(SESSION, ADDR, COND)
    try:
        task = asyncio.create_task(wf.wait_breakpoint(session_id=SESSION, timeout_s=0.5))
        await asyncio.sleep(0.1)
        _push_hit(transport)
        out = await task

        assert out["hit"] is False  # never surfaced as a hit
        assert out["filtered_hits"] == 1  # counted on the timeout path
        assert client.resume_calls == 1  # CPU auto-resumed
        assert cond_filter.get(SESSION, ADDR)["filtered"] == 1
        assert cond_filter.get(SESSION, ADDR)["hits"] == 0
    finally:
        await observer.stop()


@pytest.mark.asyncio
async def test_true_hit_returns_condition_and_filtered_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two falsy hits, then a truthy hit → normal return, filtered==2."""
    transport = _ObserverTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    client = _StubClient()
    client.evaluate_values = [0, 0, 1]
    _patch_workflows(monkeypatch, client, observer)
    cond_filter.register(SESSION, ADDR, COND)
    try:
        task = asyncio.create_task(wf.wait_breakpoint(session_id=SESSION, timeout_s=2.0))
        await asyncio.sleep(0.1)
        _push_hit(transport)
        _push_hit(transport)
        _push_hit(transport)
        out = await task

        assert out["hit"] is True
        assert out["condition"] == COND
        assert out["condition_filtered"] == 2  # falsy hits before this one
        assert out["storm_break"] is False
        assert client.resume_calls == 2
        assert cond_filter.get(SESSION, ADDR)["hits"] == 1
    finally:
        await observer.stop()


@pytest.mark.asyncio
async def test_evaluate_failure_is_treated_conservatively_as_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A WS/parse failure must NOT silently drop the hit — return it + note."""
    transport = _ObserverTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    client = _StubClient()

    async def boom(expression, thread=None):
        raise RuntimeError("ws down")

    client.evaluate = boom  # type: ignore[method-assign]
    _patch_workflows(monkeypatch, client, observer)
    cond_filter.register(SESSION, ADDR, COND)
    try:
        task = asyncio.create_task(wf.wait_breakpoint(session_id=SESSION, timeout_s=1.0))
        await asyncio.sleep(0.1)
        _push_hit(transport)
        out = await task

        assert out["hit"] is True
        assert out.get("note"), "eval failure must be explained in note"
        assert client.resume_calls == 0
    finally:
        await observer.stop()


# ── (d) storm breaker ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hit_storm_breaks_and_disarms(monkeypatch: pytest.MonkeyPatch) -> None:
    """≥10 hits within <1s gaps → breakpoint removed, filter dropped."""
    transport = _ObserverTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    client = _StubClient()
    client.evaluate_values = [0]  # condition never holds
    _patch_workflows(monkeypatch, client, observer)
    cond_filter.register(SESSION, ADDR, COND)
    try:
        task = asyncio.create_task(wf.wait_breakpoint(session_id=SESSION, timeout_s=5.0))
        await asyncio.sleep(0.1)
        for _ in range(10):
            _push_hit(transport)
        out = await task

        assert out["storm_break"] is True
        assert out["hit"] is False
        assert client.cpu_removes == [ADDR]  # breakpoint disarmed
        assert cond_filter.get(SESSION, ADDR) is None  # filter dropped
        assert out.get("note")
    finally:
        await observer.stop()


# ── (e) session stop drops filters ──────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_session_drops_session_filters(isolated_sessions_path: Path) -> None:
    sess = Session(
        session_id=SESSION,
        iso_path="/tmp/fake.iso",
        pid=None,
        ws_url="ws://127.0.0.1:1/debugger",
        created_at=datetime.now(UTC),
        last_active_at=datetime.now(UTC),
    )
    isolated_sessions_path.parent.mkdir(parents=True, exist_ok=True)
    import json

    isolated_sessions_path.write_text(
        json.dumps({SESSION: sm._session_to_dict(sess)}), encoding="utf-8"
    )
    cond_filter.register(SESSION, ADDR, COND)

    manager = sm.SessionManager()
    await manager.stop_session(SESSION)

    assert cond_filter.get(SESSION, ADDR) is None
