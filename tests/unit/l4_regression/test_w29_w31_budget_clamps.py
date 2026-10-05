"""W29/W31 regression: a declared budget must bound the calls it covers.

W29 (report §W29): three polling loops declared a wall-clock budget but left
each poll on ``WsTransport.call``'s default 5s timeout, and only examined the
deadline AFTER a successful round-trip. A ``pause()`` that promised 3000ms
could therefore take 5.5s+ to report, ``replay_wait_complete``'s declared 10s
took ~15s, and the boot probe added one default call timeout on top of every
deadline check. ``replay.timeout_ms`` even advertised ``le=60000`` although the
MCP client aborts a tool call at ~30s, so anything above that could never be
honoured.

The fix routes every one of those numbers through one name
(``DEFAULT_CALL_TIMEOUT_S``) and clamps each poll to what is left of the
caller's budget:

    status = await self.call(
        "cpu.status", timeout=max(0.1, min(DEFAULT_CALL_TIMEOUT_S, remaining_s))
    )

W31 (report §W31): the foreground admission gate estimated a batch's cost with
a floor of 0.25s per state_probe while the probe step itself could wait the
tool's full 30s ceiling, so a batch admitted at 24s could still hold the session
lock far past the budget the client was promised. The probe ceiling is now
``min(PROBE_OBSERVE_BUDGET_S, batch deadline - now)``, computed from the same
budget the gate used.

Anchors: ``src/ppsspp_dfx_mcp/core/transport.py`` (``DEFAULT_CALL_TIMEOUT_S``,
``call``, ``wait_for_state``), ``service/debug_client.py``
(``replay_wait_complete``), ``session/safe_boot.py`` (``probe_cpu_ready``),
``tools/state_observer.py`` (``PROBE_OBSERVE_BUDGET_S``),
``tools/batch_step.py`` (``_per_step_timeout``, ``_execute_batch``),
``tools/replay.py`` (``timeout_ms`` field).
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

import pytest
from websockets.protocol import State

import ppsspp_dfx_mcp.service.probe_observer as so
import ppsspp_dfx_mcp.tools.batch_step as bs
from ppsspp_dfx_mcp.core.batch_jobs import FOREGROUND_BUDGET_S
from ppsspp_dfx_mcp.core.transport import DEFAULT_CALL_TIMEOUT_S, WS_SUBPROTOCOL, WsTransport
from ppsspp_dfx_mcp.errors import BootTimeout
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient
from ppsspp_dfx_mcp.session.safe_boot import probe_cpu_ready
from ppsspp_dfx_mcp.tools.batch_step import _execute_batch, _per_step_timeout
from ppsspp_dfx_mcp.tools.state_observer import PROBE_OBSERVE_BUDGET_S


class _SilentWs:
    """A socket that accepts sends and never answers (an unresponsive PPSSPP)."""

    subprotocol = WS_SUBPROTOCOL
    state = State.OPEN

    async def send(self, data: str) -> None:  # pragma: no cover - accepted, ignored
        return None

    async def recv(self) -> str:
        # Never resolves: the peer is wedged. Cancellation-safe because the
        # Event is created per call and nothing is popped from a queue.
        await asyncio.Event().wait()
        raise AssertionError("unreachable")  # pragma: no cover

    async def close(self) -> None:  # pragma: no cover - not used by these tests
        self.state = State.CLOSED


class _HangingTransport:
    """Stub transport whose every call burns its own timeout, then reports no response."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.events: list[str] = []

    async def call(
        self, event: str, timeout: float = DEFAULT_CALL_TIMEOUT_S, **params: Any
    ) -> dict[str, Any]:
        self.events.append(event)
        self.timeouts.append(timeout)
        await asyncio.sleep(timeout)
        raise TimeoutError(f"no response within {timeout}s")


# ============================================================================
# W31: the per-step probe ceiling
# ============================================================================


class TestProbeStepTimeout:
    """``_per_step_timeout`` is the single place the W31 ceiling is derived."""

    def test_no_deadline_keeps_the_shared_tool_ceiling(self) -> None:
        assert _per_step_timeout(None, PROBE_OBSERVE_BUDGET_S) == PROBE_OBSERVE_BUDGET_S

    def test_far_deadline_keeps_the_ceiling(self) -> None:
        # A background batch's budget is an hour out: the tool ceiling governs.
        assert (
            _per_step_timeout(time.monotonic() + 3600, PROBE_OBSERVE_BUDGET_S)
            == PROBE_OBSERVE_BUDGET_S
        )

    def test_near_deadline_shrinks_to_what_is_left(self) -> None:
        timeout = _per_step_timeout(time.monotonic() + 5.0, PROBE_OBSERVE_BUDGET_S)
        assert 4.0 <= timeout <= 5.0

    def test_expired_deadline_keeps_a_floor(self) -> None:
        # Never 0/negative: wait_for(0) would cancel before the coroutine runs.
        assert _per_step_timeout(time.monotonic() - 1.0, PROBE_OBSERVE_BUDGET_S) == 1.0

    def test_step_ceiling_never_exceeds_the_admitting_gate(self) -> None:
        """W31: the gate that admitted the batch must bound one step."""
        assert (
            _per_step_timeout(time.monotonic() + FOREGROUND_BUDGET_S, PROBE_OBSERVE_BUDGET_S)
            <= FOREGROUND_BUDGET_S
        )

    def test_batch_call_sites_derive_the_ceiling_from_the_shared_budget(self) -> None:
        # Structural lock: the probe wait must not go back to a bare 30.0.
        source = inspect.getsource(_execute_batch)
        assert "_per_step_timeout(deadline, PROBE_OBSERVE_BUDGET_S)" in source
        assert "timeout=30.0" not in source


class TestProbeStepRespectsTheBatchDeadline:
    """End-to-end: one state_probe step cannot outlive the batch budget."""

    @pytest.mark.asyncio
    async def test_probe_step_ceiling_shrinks_with_the_batch_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import contextlib

        captured: dict[str, float] = {}
        real_wait_for = asyncio.wait_for

        class _AsyncioShim:
            def wait_for(self, awaitable: Any, timeout: float) -> Any:
                captured["timeout"] = timeout
                return real_wait_for(awaitable, timeout)

            def __getattr__(self, name: str) -> Any:
                return getattr(asyncio, name)

        class _Client:
            async def replay_status(self) -> dict[str, Any]:
                return {"saving": False, "executing": False}

        @contextlib.asynccontextmanager
        async def fake_session_client(session_id: str):
            yield _Client()

        async def fake_observe(client: Any, probes: Any, samples: Any) -> dict[str, Any]:  # noqa: ARG001
            return {"values": {}, "observed": 0}

        monkeypatch.setattr(bs, "session_client", fake_session_client)
        monkeypatch.setattr(bs, "asyncio", _AsyncioShim())
        monkeypatch.setattr(so, "_seed_from_yaml", lambda session_id: None)
        monkeypatch.setattr(so, "_resolve_target_probes", lambda names, session_id: [])
        monkeypatch.setattr(so, "_observe_probes", fake_observe)

        await _execute_batch(
            "s1", [{"type": "state_probe", "names": "x", "samples": 1}], "abort", None, 5.0
        )

        assert "timeout" in captured, "the probe step did not go through asyncio.wait_for"
        # Pre-fix this was the tool's bare 30.0 ceiling, i.e. a step could
        # outlive the 5s budget the batch was admitted under. The 1.0s floor
        # in `_per_step_timeout` is all that survives a sub-second budget.
        assert captured["timeout"] <= 5.0
        assert captured["timeout"] < PROBE_OBSERVE_BUDGET_S


# ============================================================================
# W29: polling loops honour the budget they declare
# ============================================================================


class TestWaitForStateBudget:
    """``WsTransport.wait_for_state`` must report within its own budget."""

    @pytest.mark.asyncio
    async def test_poll_call_is_clamped_to_the_remaining_budget(self) -> None:
        transport = WsTransport("127.0.0.1", 1)
        transport.ws = _SilentWs()  # type: ignore[assignment]

        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError) as ei:
            await transport.wait_for_state(lambda st: True, timeout_ms=300, interval_ms=50)
        elapsed = asyncio.get_running_loop().time() - started

        assert "wait_for_state timeout (300ms)" in str(ei.value)
        # The poll itself uses 300ms; the best-effort stepping probe adds 0.5s.
        # Pre-fix the poll used the 5s default and the whole wait took 5.5s+.
        assert elapsed < 1.5, f"wait_for_state took {elapsed:.2f}s for a 300ms budget"

    @pytest.mark.asyncio
    async def test_tight_budget_still_lets_a_satisfying_poll_win(self) -> None:
        """Clamping must not pre-empt the poll that satisfies the predicate.

        Regression lock for the first revision of this fix, which raised
        before polling once the remaining budget was <= 0 and so broke the
        ``timeout_ms=1`` tolerance documented in
        ``test_transport_orchestration.py::TestWaitForState`` ("Even with
        timeout_ms=0, predicate satisfied on 2nd poll should return").
        """
        transport = WsTransport("127.0.0.1", 1)
        calls = 0

        async def fake_call(event: str, timeout: float = 5.0, **params: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            return {"stepping": calls >= 2}

        transport.call = fake_call  # type: ignore[method-assign]

        result = await transport.wait_for_state(
            lambda st: st.get("stepping") is True, timeout_ms=1, interval_ms=1
        )

        assert result["stepping"] is True
        assert calls == 2


class TestReplayWaitCompleteBudget:
    """``DebugClient.replay_wait_complete`` must report within its own budget."""

    @pytest.mark.asyncio
    async def test_status_poll_is_clamped_to_the_remaining_budget(self) -> None:
        stub = _HangingTransport()
        client = PpssppDebugClient(stub)  # type: ignore[arg-type]

        started = asyncio.get_running_loop().time()
        with pytest.raises(TimeoutError) as ei:
            await client.replay_wait_complete(timeout_ms=250, interval_ms=10)
        elapsed = asyncio.get_running_loop().time() - started

        assert "replay.wait_complete timeout (250ms)" in str(ei.value)
        assert elapsed < 1.5, f"replay_wait_complete took {elapsed:.2f}s for a 250ms budget"
        assert stub.events == ["replay.status"]
        assert stub.timeouts[0] <= 0.25 + 1e-6


class TestProbeCpuReadyBudget:
    """``probe_cpu_ready`` must declare a wedge within its own budget."""

    @pytest.mark.asyncio
    async def test_probe_read_is_clamped_to_the_remaining_budget(self) -> None:
        stub = _HangingTransport()

        started = asyncio.get_running_loop().time()
        with pytest.raises(BootTimeout) as ei:
            await probe_cpu_ready(stub, budget_s=0.3, poll_interval_s=0.05)
        elapsed = asyncio.get_running_loop().time() - started

        assert "emulated CPU did not start within 0s" in str(ei.value)
        assert elapsed < 1.5, f"probe_cpu_ready took {elapsed:.2f}s for a 300ms budget"
        assert stub.events == ["memory.read_u32"]
        assert stub.timeouts[0] <= 0.3 + 1e-6


# ============================================================================
# W29: the advertised ceiling matches what the client can honour
# ============================================================================


class TestReplayTimeoutCeiling:
    """``replay.timeout_ms`` must not advertise a value the client will abort."""

    @pytest.mark.asyncio
    async def test_wire_schema_ceiling_is_25s(self) -> None:
        from ppsspp_dfx_mcp import server as server_mod

        server_mod.register_all_tools()
        tools = {t.name: t for t in await server_mod.mcp.list_tools()}
        prop = tools["ppsspp_replay"].input_schema["properties"]["timeout_ms"]

        assert prop["maximum"] == 25000
        assert prop["minimum"] == 100
        assert "25s" in prop["description"]
