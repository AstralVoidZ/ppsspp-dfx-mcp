"""L4 regression tests for A1/A2 step stale-broadcast filter + step_out pc validation.

A1 (2026-07-24): step_into/step_over/step_out must reject stale
``cpu.stepping`` broadcasts produced by ``pause()`` (which shares the
event name with step commands). The fix captures pre-step pc/ticks
and applies a filter that accepts only broadcasts whose pc OR ticks
differs from the pre-step value.

A2 (2026-07-24): step_out must post-validate the returned pc against
the PSP executable code range (``0x08800000``–``0x0C000000``). A pc
of ``0x08000000`` (PSP user-memory base address, used as a stack-
bottom sentinel) indicates the stack walk returned an invalid caller
frame and the consumed broadcast was a stale one.

Root-cause analysis and source-level evidence:
- .tmp/test_unresolved.log (2026-07-24 04:28:28 WS packet capture)
"""

from __future__ import annotations

from typing import Any

import pytest

from fake_transport import FakeTransport
from ppsspp_dfx_mcp.errors import StepNoAdvanceError, StepOutError
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


# ---------- Test fixtures ----------


def _pause_keep_pc_ticks(t: FakeTransport, **params: Any) -> None:
    """Pause handler that preserves pc/ticks across the stepping transition.

    Mirrors the real PPSSPP behavior: pause() enters stepping but does
    not advance pc or ticks. The default FakeTransport fixture's
    ``_set_stepping_true`` handler replaces the entire state with
    ``{"stepping": True}``, losing pc/ticks — that would disable the
    A1 filter (pre_pc=None). This handler preserves them so the filter
    can be exercised.
    """
    cur = t.state
    t.set_state({
        "stepping": True,
        "pc": cur.get("pc", 0),
        "ticks": cur.get("ticks", 0.0),
    })


@pytest.fixture
def transport() -> FakeTransport:
    """FakeTransport with realistic cpu.status (pc + ticks + stepping).

    Initial state: stepping=False, pc=0x08804000, ticks=100.0.
    The pause faf handler preserves pc/ticks so the A1 filter can
    compare against the pre-step values.
    """
    t = FakeTransport()
    t.set_state({"stepping": False, "pc": 0x08804000, "ticks": 100.0})
    t.set_faf_handler("cpu.stepping", _pause_keep_pc_ticks)
    return t


@pytest.fixture
def client(transport: FakeTransport) -> PpssppDebugClient:
    return PpssppDebugClient(transport)


# ---------- A1: stale-broadcast filter ----------


class TestA1StaleBroadcastFilter:
    """A1: step methods reject stale pause() broadcasts.

    PPSSPP's ``cpu.stepping`` event name is shared between pause()
    confirmation and step completion. Without a filter, the stale
    pause broadcast is consumed by step's wait_for_broadcast, causing
    "fake success" (step returns the pre-step pc/ticks, caller cannot
    tell the CPU didn't advance).
    """

    @pytest.mark.asyncio
    async def test_step_into_rejects_stale_pause_broadcast(
        self, client: PpssppDebugClient, transport: FakeTransport
    ) -> None:
        """A1: step_into rejects a broadcast whose pc AND ticks match pre-step.

        Simulates the real-world failure: pause() produces a
        cpu.stepping broadcast with pc=pre_pc, ticks=pre_ticks (the
        CPU didn't execute anything). The filter must reject it and
        surface a TimeoutError rather than fake success.
        """

        def _step_faf_stale(t: FakeTransport, **params: Any) -> None:
            # Push a stale broadcast (pc/ticks == pre-step values).
            t.push_broadcast({
                "event": "cpu.stepping",
                "pc": 0x08804000,  # same as pre-step
                "ticks": 100.0,    # same as pre-step
                "reason": "cpu.stepInto",
                "relatedAddress": 0,
            })

        transport.set_faf_handler("cpu.stepInto", _step_faf_stale)
        # F-4 fix (2026-09-06): repeated no-op broadcasts surface as a
        # decisive StepNoAdvanceError (with retry) instead of a blind
        # TimeoutError — the no-fake-success guarantee is preserved.
        # W10b fix: StepNoAdvanceError is a ToolError (code
        # STEP_NO_ADVANCE) so the diagnosis is no longer misclassified as
        # WsDisconnected by to_tool_error.
        with pytest.raises(StepNoAdvanceError):
            await client.step_into(timeout_ms=300, interval_ms=10)

    @pytest.mark.asyncio
    async def test_step_into_accepts_fresh_step_broadcast(
        self, client: PpssppDebugClient, transport: FakeTransport
    ) -> None:
        """A1: step_into accepts a broadcast whose pc differs from pre-step.

        Verifies the filter does not over-reject: a real step
        completion (pc advanced by one instruction) must pass through.
        """

        def _step_faf_fresh(t: FakeTransport, **params: Any) -> None:
            # Push a fresh broadcast (pc advanced, ticks advanced).
            t.push_broadcast({
                "event": "cpu.stepping",
                "pc": 0x08804008,  # +8 (2 MIPS instructions)
                "ticks": 102.0,    # +2 cycles
                "reason": "cpu.stepInto",
                "relatedAddress": 0,
            })

        transport.set_faf_handler("cpu.stepInto", _step_faf_fresh)
        result = await client.step_into(timeout_ms=500, interval_ms=10)
        assert result["pc"] == 0x08804008
        assert result["ticks"] == 102.0

    @pytest.mark.asyncio
    async def test_step_into_running_state_auto_pauses(
        self, client: PpssppDebugClient, transport: FakeTransport
    ) -> None:
        """A1 fix for root cause B: step_into on running CPU auto-pauses first.

        Without this, stepOver/stepOut hit PPSSPP's REQUIRED_STEPPING
        check and fail with a ticketed error that fire_and_forget
        ignores, causing a 6125ms double-timeout. step_into doesn't
        fail (no REQUIRED_STEPPING check) but produces no broadcast
        when running, also timing out. The fix calls pause() first.
        """
        # Initial state: stepping=False (running).
        assert transport.state["stepping"] is False

        def _step_faf(t: FakeTransport, **params: Any) -> None:
            t.push_broadcast({
                "event": "cpu.stepping",
                "pc": 0x08804008,
                "ticks": 102.0,
                "reason": "cpu.stepInto",
                "relatedAddress": 0,
            })

        transport.set_faf_handler("cpu.stepInto", _step_faf)
        await client.step_into(timeout_ms=500, interval_ms=10)

        # Verify pause was called before stepInto.
        faf_events = [e for e, _ in transport.fire_and_forget_calls]
        assert "cpu.stepping" in faf_events, (
            "step_into must call pause() (fire cpu.stepping) before "
            "cpu.stepInto when CPU is running — A1 root cause B fix."
        )
        assert "cpu.stepInto" in faf_events
        # Pause must come before stepInto.
        assert faf_events.index("cpu.stepping") < faf_events.index("cpu.stepInto")

    @pytest.mark.asyncio
    async def test_step_into_skips_pause_when_already_stepping(
        self, client: PpssppDebugClient, transport: FakeTransport
    ) -> None:
        """A1: when CPU is already stepping, step_into must not call pause().

        Avoids redundant pause calls that would produce extra stale
        broadcasts.
        """
        transport.set_state({"stepping": True, "pc": 0x08804000, "ticks": 100.0})

        def _step_faf(t: FakeTransport, **params: Any) -> None:
            t.push_broadcast({
                "event": "cpu.stepping",
                "pc": 0x08804008,
                "ticks": 102.0,
                "reason": "cpu.stepInto",
                "relatedAddress": 0,
            })

        transport.set_faf_handler("cpu.stepInto", _step_faf)
        await client.step_into(timeout_ms=500, interval_ms=10)

        faf_events = [e for e, _ in transport.fire_and_forget_calls]
        # Only cpu.stepInto should be fired — no cpu.stepping (pause).
        assert "cpu.stepping" not in faf_events, (
            "step_into must not call pause() when CPU is already "
            "stepping — would produce an extra stale broadcast."
        )
        assert faf_events == ["cpu.stepInto"]


class TestA1BuildStepFilterUnit:
    """Unit tests for PpssppDebugClient._build_step_filter."""

    def test_returns_none_when_no_pre_state(self) -> None:
        """No pre_pc AND no pre_ticks → filter disabled (None)."""
        assert PpssppDebugClient._build_step_filter(None, None) is None

    def test_returns_none_when_both_none(self) -> None:
        """Explicit None for both → None (backward compat)."""
        assert PpssppDebugClient._build_step_filter(None, None) is None

    def test_rejects_broadcast_matching_pre_pc_and_ticks(self) -> None:
        """Filter rejects broadcasts whose pc AND ticks match pre-step."""
        f = PpssppDebugClient._build_step_filter(0x08804000, 100.0)
        assert f is not None
        assert f({"pc": 0x08804000, "ticks": 100.0}) is False

    def test_accepts_broadcast_with_changed_pc(self) -> None:
        """Filter accepts broadcasts whose pc differs from pre-step."""
        f = PpssppDebugClient._build_step_filter(0x08804000, 100.0)
        assert f is not None
        assert f({"pc": 0x08804008, "ticks": 100.0}) is True

    def test_accepts_broadcast_with_changed_ticks(self) -> None:
        """Filter accepts broadcasts whose ticks differ from pre-step."""
        f = PpssppDebugClient._build_step_filter(0x08804000, 100.0)
        assert f is not None
        assert f({"pc": 0x08804000, "ticks": 101.0}) is True

    def test_only_pre_pc_known_accepts_any_ticks(self) -> None:
        """When only pre_pc is known, ticks is treated as wildcards."""
        f = PpssppDebugClient._build_step_filter(0x08804000, None)
        assert f is not None
        # pc matches pre_pc → rejected regardless of ticks.
        assert f({"pc": 0x08804000, "ticks": 999.0}) is False
        # pc differs → accepted regardless of ticks.
        assert f({"pc": 0x08804008, "ticks": 100.0}) is True

    def test_only_pre_ticks_known_accepts_any_pc(self) -> None:
        """When only pre_ticks is known, pc is treated as wildcards."""
        f = PpssppDebugClient._build_step_filter(None, 100.0)
        assert f is not None
        assert f({"pc": 0x08804000, "ticks": 100.0}) is False
        assert f({"pc": 0x99999999, "ticks": 101.0}) is True


# ---------- A2: step_out pc range validation ----------


class TestA2StepOutPcValidation:
    """A2: step_out post-validates returned pc against PSP code range.

    PSP user-memory executable range: 0x08800000–0x0C000000.
    A returned pc of 0x08000000 (PSP RAM base) indicates the stack
    walk returned a stack-bottom sentinel and the consumed broadcast
    was a stale one.
    """

    @pytest.mark.asyncio
    async def test_step_out_raises_on_psp_ram_base_pc(
        self, client: PpssppDebugClient, transport: FakeTransport
    ) -> None:
        """A2: pc=0x08000000 (PSP RAM base) → StepOutError.

        Reproduces the real-world failure observed in
        .tmp/test_unresolved.log TEST 4: step_out returned
        pc=134217760=0x08000000 in 15ms (impossibly fast for a real
        step-out), confirming a stale broadcast was consumed.
        """

        def _step_faf_invalid(t: FakeTransport, **params: Any) -> None:
            t.push_broadcast({
                "event": "cpu.stepping",
                "pc": 0x08000000,  # PSP RAM base — invalid code addr
                "ticks": 102.0,
                "reason": "cpu.stepOut",
                "relatedAddress": 0,
            })

        transport.set_faf_handler("cpu.stepOut", _step_faf_invalid)
        with pytest.raises(StepOutError) as exc_info:
            await client.step_out(timeout_ms=500, interval_ms=10)
        assert "0x08000000" in str(exc_info.value)
        assert "0x08800000" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_step_out_passes_on_valid_pc(
        self, client: PpssppDebugClient, transport: FakeTransport
    ) -> None:
        """A2: pc inside 0x08800000–0x0C000000 → no error."""

        def _step_faf_valid(t: FakeTransport, **params: Any) -> None:
            t.push_broadcast({
                "event": "cpu.stepping",
                "pc": 0x08804000,  # top.prx base — valid
                "ticks": 102.0,
                "reason": "cpu.stepOut",
                "relatedAddress": 0,
            })

        transport.set_faf_handler("cpu.stepOut", _step_faf_valid)
        result = await client.step_out(timeout_ms=500, interval_ms=10)
        assert result["pc"] == 0x08804000

    @pytest.mark.asyncio
    async def test_step_out_passes_on_upper_bound_pc(
        self, client: PpssppDebugClient, transport: FakeTransport
    ) -> None:
        """A2: pc=0x0C000000 (inclusive upper bound) → no error."""

        def _step_faf_upper(t: FakeTransport, **params: Any) -> None:
            t.push_broadcast({
                "event": "cpu.stepping",
                "pc": 0x0C000000,  # inclusive upper bound
                "ticks": 102.0,
                "reason": "cpu.stepOut",
                "relatedAddress": 0,
            })

        transport.set_faf_handler("cpu.stepOut", _step_faf_upper)
        result = await client.step_out(timeout_ms=500, interval_ms=10)
        assert result["pc"] == 0x0C000000

    @pytest.mark.asyncio
    async def test_step_out_raises_on_above_range_pc(
        self, client: PpssppDebugClient, transport: FakeTransport
    ) -> None:
        """A2: pc above 0x0C000000 → StepOutError (kernel/devkit memory)."""

        def _step_faf_above(t: FakeTransport, **params: Any) -> None:
            t.push_broadcast({
                "event": "cpu.stepping",
                "pc": 0x0C000001,  # just above range
                "ticks": 102.0,
                "reason": "cpu.stepOut",
                "relatedAddress": 0,
            })

        transport.set_faf_handler("cpu.stepOut", _step_faf_above)
        with pytest.raises(StepOutError):
            await client.step_out(timeout_ms=500, interval_ms=10)

    @pytest.mark.asyncio
    async def test_step_out_skips_validation_when_pc_missing(
        self, client: PpssppDebugClient, transport: FakeTransport
    ) -> None:
        """A2: broadcast without pc field → skip validation (defensive).

        Some legacy PPSSPP builds may omit pc from the broadcast.
        Skip validation rather than fail — the caller can still use
        the broadcast's other fields.
        """

        def _step_faf_no_pc(t: FakeTransport, **params: Any) -> None:
            t.push_broadcast({
                "event": "cpu.stepping",
                "ticks": 102.0,
                "reason": "cpu.stepOut",
                "relatedAddress": 0,
                # No "pc" key.
            })

        transport.set_faf_handler("cpu.stepOut", _step_faf_no_pc)
        result = await client.step_out(timeout_ms=500, interval_ms=10)
        assert "pc" not in result
