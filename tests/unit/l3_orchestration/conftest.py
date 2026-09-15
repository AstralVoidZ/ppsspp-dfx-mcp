"""L3 orchestration test fixtures.

L3 tests anchor cross-method orchestration behavior (B.2 spec 17
invariants I1-I17 + 5 documented decisions V022/V025/V026/V024/V027).
Complementary to L4 regression tests:

- L4 anchors violation numbers (isolated invariants per V004/V020/V023)
- L3 anchors spec invariants under cross-method orchestration
  (e.g. step_into → _confirm_step_completed → broadcast subscription;
  scan_memory chunked reads + short-read cursor advance;
  CaptureService three-tier degradation chain).

Reuses the canonical FakeTransport from `tests/fake_transport/`
(injected to sys.path by root conftest.py). Step-method faf handlers
push `cpu.stepping` broadcasts (V004 redesign stage 5 path).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from fake_transport import FakeTransport
from ppsspp_dfx_mcp.core.stepping import SteppingManager
from ppsspp_dfx_mcp.service.capture import CaptureService
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


# ---------- Shared faf handlers ----------


def _set_stepping_true(t: FakeTransport, **params: Any) -> None:
    cur = t.state
    t.set_state({**cur, "stepping": True})


def _set_stepping_false(t: FakeTransport, **params: Any) -> None:
    cur = t.state
    t.set_state({**cur, "stepping": False})


def _step_then_pause(t: FakeTransport, **params: Any) -> None:
    """Simulate step execution: push cpu.stepping broadcast + set stepping=True.

    V004 stage 5 (B.2 redesign): _confirm_step_completed subscribes
    to the `cpu.stepping` broadcast via `transport.wait_for_broadcast`.
    This handler pushes a broadcast message to the events queue and
    also sets the legacy `stepping=True` state (for the legacy fallback
    path / `use_broadcast=False`).
    """
    t.set_state({"stepping": False})

    async def _complete_step() -> None:
        t.push_broadcast({
            "event": "cpu.stepping",
            "pc": 0x08804000,
            "ticks": 12345.0,
            "reason": "cpu.stepInto",
            "relatedAddress": 0,
        })
        t.set_state({"stepping": True})

    asyncio.get_event_loop().call_soon(
        lambda: asyncio.ensure_future(_complete_step())
    )


# ---------- Fixtures ----------


@pytest.fixture
def transport() -> FakeTransport:
    """FakeTransport pre-configured for stepping state transitions.

    - Initial state: stepping=False (running)
    - fire_and_forget("cpu.stepping") → state stepping=True
    - fire_and_forget("cpu.resume") → state stepping=False
    - fire_and_forget("cpu.stepInto/stepOver/stepOut/runUntil/nextHLE")
      → push cpu.stepping broadcast (consumed by wait_for_broadcast)
      AND set stepping=True (legacy fallback state).
    """
    t = FakeTransport()
    t.set_state({"stepping": False})
    t.set_faf_handler("cpu.stepping", _set_stepping_true)
    t.set_faf_handler("cpu.resume", _set_stepping_false)
    t.set_faf_handler("cpu.stepInto", _step_then_pause)
    t.set_faf_handler("cpu.stepOver", _step_then_pause)
    t.set_faf_handler("cpu.stepOut", _step_then_pause)
    t.set_faf_handler("cpu.runUntil", _step_then_pause)
    t.set_faf_handler("cpu.nextHLE", _step_then_pause)
    return t


@pytest.fixture
def client(transport: FakeTransport) -> PpssppDebugClient:
    """PpssppDebugClient backed by FakeTransport."""
    return PpssppDebugClient(transport)


@pytest.fixture
def manager(transport: FakeTransport) -> SteppingManager:
    """SteppingManager with short timeout/interval for fast tests."""
    return SteppingManager(
        transport,
        default_timeout_ms=500,
        default_interval_ms=5,
    )


@pytest.fixture
def capture(client: PpssppDebugClient, transport: FakeTransport) -> CaptureService:
    """CaptureService with explicitly-injected transport (V023 fix).

    V023 (B.2 §4): CaptureService requires explicit transport injection.
    The same FakeTransport that backs the client is passed so tests can
    configure gpu.buffer.screenshot responses via `transport.set_response`.
    """
    return CaptureService(client, transport=transport)
