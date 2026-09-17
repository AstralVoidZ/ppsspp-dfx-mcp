"""H0 acceptance tests: ppsspp_session(action='wait_ready').

A-H0-1: fake mode is ready immediately; production mode returns the
probed word once the CPU-start probe succeeds.
A-H0-2 (translation half): "CPU not started" PPSSPP errors carry the
wait_ready hint; the probe budget exhausts into [BOOT_TIMEOUT] with a
wedge-recovery hint.
"""

from __future__ import annotations

import pytest

import ppsspp_dfx_mcp.tools.session as session_tool
from ppsspp_dfx_mcp.errors import (
    BootTimeout,
    PpssppProtocolError,
    SessionNotFound,
    to_tool_error,
)


async def _async_noop(*args, **kwargs) -> None:
    return None


class _StubTransport:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    async def call(self, event: str, **params):
        self.calls.append((event, params))
        if self.fail:
            raise RuntimeError("PPSSPP error: CPU not started (level=2)")
        return {"value": 0x12345678}


def _patch_production(monkeypatch: pytest.MonkeyPatch, transport: _StubTransport) -> None:
    """Common production-mode patching: no fake mode, liveness noop,
    session_manager.get_transport returns the stub."""
    monkeypatch.setattr(session_tool, "test_mode", lambda: "")
    monkeypatch.setattr(session_tool, "validate_session_alive", _async_noop)

    async def fake_get_transport(session_id: str):
        return transport

    monkeypatch.setattr(session_tool.session_manager, "get_transport", fake_get_transport)


@pytest.mark.asyncio
async def test_wait_ready_fake_mode_ready_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-H0-1 (fake half): fake test mode has no boot concept — the tool
    must short-circuit to ready without touching any transport."""
    monkeypatch.setattr(session_tool, "test_mode", lambda: "fake")
    out = await session_tool.session(action="wait_ready", session_id="sess-fake")
    assert out["ready"] is True
    assert out["action"] == "wait_ready"
    assert "fake" in (out["note"] or "")
    assert out["probe_value"] is None


@pytest.mark.asyncio
async def test_wait_ready_returns_probed_word_when_cpu_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A-H0-1 (production half): first successful probe read returns the
    probed word and the elapsed time."""
    transport = _StubTransport()
    _patch_production(monkeypatch, transport)
    out = await session_tool.session(action="wait_ready", session_id="sess-1", timeout_s=5.0)
    assert out["ready"] is True
    assert out["probe_value"] == "0x12345678"
    assert out["probe_addr"] == "0x08804000"
    assert transport.calls[0][0] == "memory.read_u32"


@pytest.mark.asyncio
async def test_wait_ready_times_out_into_boot_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A never-starting CPU (wedge family) exhausts the budget into
    BootTimeout with wedge-recovery guidance, not a generic timeout."""
    transport = _StubTransport(fail=True)
    _patch_production(monkeypatch, transport)
    monkeypatch.setattr(session_tool, "_WAIT_READY_POLL_INTERVAL_S", 0.01)
    with pytest.raises(BootTimeout) as ei:
        await session_tool.session(action="wait_ready", session_id="sess-1", timeout_s=0.05)
    msg = str(ei.value)
    assert "[BOOT_TIMEOUT]" in msg
    assert "ppsspp_analyze_log" in msg
    assert "restart" in msg


@pytest.mark.asyncio
async def test_wait_ready_session_gone_aborts_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SessionNotFound during polling aborts instead of polling forever."""
    monkeypatch.setattr(session_tool, "test_mode", lambda: "")
    monkeypatch.setattr(session_tool, "validate_session_alive", _async_noop)

    async def fake_get_transport(session_id: str):
        raise SessionNotFound("gone")

    monkeypatch.setattr(session_tool.session_manager, "get_transport", fake_get_transport)
    with pytest.raises(SessionNotFound):
        await session_tool.session(action="wait_ready", session_id="sess-1", timeout_s=5.0)


def test_cpu_not_started_error_carries_wait_ready_hint() -> None:
    """A-H0-2 (translation half): an early read failing with PPSSPP's
    'CPU not started' surfaces the wait_ready next step to the agent."""
    err = to_tool_error(RuntimeError("PPSSPP error: CPU not started (level=2)"))
    assert isinstance(err, PpssppProtocolError)
    text = str(err)
    assert "wait_ready" in text
    assert text.startswith("[PPSSPP_PROTOCOL_ERROR]")
