"""H1 acceptance tests: ppsspp_wait_breakpoint / ppsspp_trace_memory_access.

A-H1-1: a buffered broadcast satisfies wait_breakpoint with the full hit
tuple; timeout returns hit=false (NOT an error).
A-H1-4: trace_memory_access full chain on stubs — capture, list-verified
breakpoint removal, CPU resume; exception paths still clean up.
A-H1-2's real-wire half (concurrent read during the wait) is proven by
the harness race scenario; here the lock-free wait is covered by the
PARTIAL_HOLD tripwire classification.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

import ppsspp_dfx_mcp.tools.workflows as wf
from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver
from ppsspp_dfx_mcp.errors import ToolError

TRACE_ADDR = "0x08A0D000"


class _ObserverTransport:
    """Observer input: the dispatcher consumes .events; start() calls
    broadcast.config.set through call()."""

    def __init__(self) -> None:
        self.events: asyncio.Queue[dict] = asyncio.Queue()

    async def call(self, event: str, **params):
        return {}


class _StubClient:
    def __init__(self, stepping: bool = False, fail_regs: bool = False):
        self._stepping = stepping
        self._fail_regs = fail_regs
        self.ops: list = []
        self.mem_bps: dict[int, int] = {}

    async def safe_get_pc(self):
        self.ops.append("safe_get_pc")
        return 0x08812345, "high"

    async def mem_bp_add(self, address, size, read=True, write=True,
                         enabled=True, log=False, **kw):
        self.ops.append(("mem_add", address, size, read, write))
        self.mem_bps[address] = size
        return {}

    async def mem_bp_list(self):
        self.ops.append("mem_list")
        return {"breakpoints": [
            {"address": a, "size": s, "read": True, "write": True,
             "hits": 2}
            for a, s in self.mem_bps.items()
        ]}

    async def mem_bp_remove(self, address, size):
        self.ops.append(("mem_remove", address, size))
        self.mem_bps.pop(address, None)
        return {}

    async def get_all_regs(self):
        self.ops.append("get_all_regs")
        if self._fail_regs:
            raise RuntimeError("regs boom")
        return {"categories": []}

    async def backtrace(self):
        self.ops.append("backtrace")
        return {"frames": []}

    async def pause(self):
        self.ops.append("pause")
        self._stepping = True
        return {"stepping": True}

    async def resume(self):
        self.ops.append("resume")
        self._stepping = False
        return {}

    def op_names(self) -> list[str]:
        return [o if isinstance(o, str) else o[0] for o in self.ops]


class _StubTransport:
    def __init__(self, client: _StubClient):
        self._client = client

    async def call(self, event: str, **params):
        assert event == "cpu.status"
        return {"stepping": self._client._stepping}


def _patch(monkeypatch, client: _StubClient, observer: GameStateObserver):
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
    monkeypatch.setattr(wf.session_manager, "get_observer",
                        fake_get_observer)


def _push_hit(transport: _ObserverTransport) -> None:
    transport.events.put_nowait({
        "event": "cpu.stepping",
        "pc": 0x08804008,
        "relatedAddress": 0x08A0D000,
        "reason": "memory.breakpoint",
        "ticks": 1234.5,
    })


# ── ppsspp_wait_breakpoint ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_breakpoint_returns_hit_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-H1-1: the buffered broadcast satisfies the wait with the full
    hit tuple, and the subscription is released afterwards."""
    transport = _ObserverTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    client = _StubClient()
    _patch(monkeypatch, client, observer)
    try:
        task = asyncio.create_task(
            wf.wait_breakpoint(session_id="s1", timeout_s=5.0)
        )
        await asyncio.sleep(0.15)  # let the tool enter its wait
        _push_hit(transport)
        out = await task
        assert out["hit"] is True
        assert out["already_paused"] is False
        assert out["pc"] == "0x08804008"
        assert out["related_address"] == "0x08A0D000"
        assert out["reason"] == "memory.breakpoint"
        assert out["ticks"] == 1234.5
        assert observer._stepping_subscribers == []
    finally:
        await observer.stop()


@pytest.mark.asyncio
async def test_wait_breakpoint_timeout_returns_hit_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-H1-1 (timeout half): hit=false is a RESULT, not an error — the
    caller can poll."""
    transport = _ObserverTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    client = _StubClient()
    _patch(monkeypatch, client, observer)
    try:
        out = await wf.wait_breakpoint(session_id="s1", timeout_s=0.5)
        assert out["hit"] is False
        assert out["already_paused"] is False
        assert out["timeout_s"] == 0.5
        assert observer._stepping_subscribers == []
    finally:
        await observer.stop()


@pytest.mark.asyncio
async def test_wait_breakpoint_reports_already_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CPU found paused at entry → hit=true + already_paused=true with a
    high-trust pc (the hit happened before the tool call)."""
    transport = _ObserverTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    client = _StubClient(stepping=True)
    _patch(monkeypatch, client, observer)
    try:
        out = await wf.wait_breakpoint(session_id="s1", timeout_s=5.0)
        assert out["hit"] is True
        assert out["already_paused"] is True
        assert out["pc"] == "0x08812345"
        assert client.op_names() == ["safe_get_pc"]
    finally:
        await observer.stop()


# ── ppsspp_trace_memory_access ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_trace_full_chain_captures_cleans_restores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-H1-4: hit → capture (registers+backtrace) → list-verified bp
    removal → resume. Removal happens BEFORE resume (no re-hit window)."""
    transport = _ObserverTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    client = _StubClient()
    _patch(monkeypatch, client, observer)
    try:
        task = asyncio.create_task(wf.trace_memory_access(
            session_id="s1", address=TRACE_ADDR, access="read_write",
            timeout_s=5.0, want_registers=True, want_backtrace=True,
        ))
        await asyncio.sleep(0.15)
        _push_hit(transport)
        out = await task
        assert out["hit"] is True
        assert out["bp_removed"] is True
        assert out["resumed"] is True
        hit = out["hits"][0]
        assert hit["pc"] == "0x08804008"
        assert hit["related_address"] == "0x08A0D000"
        assert hit["mem_hits"] == 2  # S3: counter read BEFORE removal
        assert "registers" in hit and "backtrace" in hit
        names = client.op_names()
        assert names[0] == "mem_add"
        # add(read=True, write=True, size=4)
        assert client.ops[0] == ("mem_add", 0x08A0D000, 4, True, True)
        # capture → remove (real size) → verify → resume
        assert names[1:] == [
            "get_all_regs", "backtrace",
            "mem_list", "mem_remove", "mem_list", "resume",
        ]
        assert ("mem_remove", 0x08A0D000, 4) in client.ops
        assert observer._stepping_subscribers == []
    finally:
        await observer.stop()


@pytest.mark.asyncio
async def test_trace_timeout_removes_breakpoint_without_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No hit: the armed breakpoint is still removed; the CPU was never
    paused so resume must NOT be issued."""
    transport = _ObserverTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    client = _StubClient()
    _patch(monkeypatch, client, observer)
    try:
        out = await wf.trace_memory_access(
            session_id="s1", address=TRACE_ADDR, access="read",
            timeout_s=0.5,
        )
        assert out["hit"] is False
        assert out["bp_removed"] is True
        assert out["resumed"] is False
        names = client.op_names()
        assert names[0] == "mem_add"
        assert "mem_remove" in names
        assert "resume" not in names
    finally:
        await observer.stop()


@pytest.mark.asyncio
async def test_trace_already_paused_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CPU paused at arm → nothing can hit: no breakpoint is armed, the
    answer says so instead of burning the whole budget."""
    transport = _ObserverTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    client = _StubClient(stepping=True)
    _patch(monkeypatch, client, observer)
    try:
        out = await wf.trace_memory_access(
            session_id="s1", address=TRACE_ADDR, timeout_s=5.0,
        )
        assert out["hit"] is False
        assert out["already_paused"] is True
        assert out["bp_removed"] is False
        assert client.ops == []
        assert "resume" in (out["note"] or "")
    finally:
        await observer.stop()


@pytest.mark.asyncio
async def test_trace_capture_failure_still_cleans_up_and_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exception between hit and resume must not leave the game frozen
    with our breakpoint armed: remove + resume happen on the error path."""
    transport = _ObserverTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    client = _StubClient(fail_regs=True)
    _patch(monkeypatch, client, observer)
    try:
        task = asyncio.create_task(wf.trace_memory_access(
            session_id="s1", address=TRACE_ADDR, timeout_s=5.0,
            want_registers=True,
        ))
        await asyncio.sleep(0.15)
        _push_hit(transport)
        with pytest.raises(ToolError):
            await task
        names = client.op_names()
        assert ("mem_remove", 0x08A0D000, 4) in client.ops
        assert "resume" in names
        assert observer._stepping_subscribers == []
    finally:
        await observer.stop()


@pytest.mark.asyncio
async def test_trace_rejects_bad_size_and_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _ObserverTransport()
    observer = GameStateObserver(transport)
    await observer.start()
    client = _StubClient()
    _patch(monkeypatch, client, observer)
    try:
        with pytest.raises(ToolError, match="size"):
            await wf.trace_memory_access(
                session_id="s1", address=TRACE_ADDR, size=3,
            )
        with pytest.raises(ToolError, match="address"):
            await wf.trace_memory_access(
                session_id="s1", address="0x0",
            )
        assert client.ops == []
    finally:
        await observer.stop()


# ── ppsspp_frame_snapshot (P4) ───────────────────────────────────────────


class TestFrameSnapshot:
    def _patch(self, monkeypatch, client):
        @asynccontextmanager
        async def fake_swt(session_id: str):
            yield client, _StubTransport(client)

        @asynccontextmanager
        async def fake_sc(session_id: str):
            yield client

        async def fake_alive(session_id: str) -> None:
            return None

        monkeypatch.setattr(wf, "session_client_with_transport", fake_swt)
        monkeypatch.setattr(wf, "session_client", fake_sc)
        monkeypatch.setattr(wf, "validate_session_alive", fake_alive)

    @pytest.mark.asyncio
    async def test_running_game_pauses_captures_resumes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _StubClient(stepping=False)
        self._patch(monkeypatch, client)
        out = await wf.frame_snapshot(session_id="s1")
        assert out["was_stepping"] is False
        assert out["resumed"] is True
        assert out["pc"] == "0x08812345"
        assert "registers" in out
        names = client.op_names()
        assert names[0] == "pause"
        assert names[-1] == "resume"

    @pytest.mark.asyncio
    async def test_already_paused_left_paused(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _StubClient(stepping=True)
        self._patch(monkeypatch, client)
        out = await wf.frame_snapshot(session_id="s1", want_registers=False)
        assert out["was_stepping"] is True
        assert out["resumed"] is False
        assert out["registers"] is None  # not requested
        assert client.op_names() == ["safe_get_pc"]

    @pytest.mark.asyncio
    async def test_capture_failure_resumes_our_pause(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        client = _StubClient(stepping=False, fail_regs=True)
        self._patch(monkeypatch, client)
        with pytest.raises(ToolError):
            await wf.frame_snapshot(session_id="s1")
        names = client.op_names()
        assert names[0] == "pause"
        assert "resume" in names  # recovery: our pause is undone
