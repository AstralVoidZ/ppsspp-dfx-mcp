"""L4 regression tests for V004 (broadcast subscription path).

Stage 5 redesign (B.2 spec §2.4): `_confirm_step_completed` now
subscribes to the `cpu.stepping` broadcast via
`transport.wait_for_broadcast` by default. The legacy single-poll
fallback is anchored in `test_v004_confirm_step_single_poll.py`.

Invariants anchored here (B.2 spec §2.4.4):
- I6 (返回字段): returned dict has event/pc/ticks/reason/relatedAddress.
- I7 (timeout 一致): default timeout_ms=5000.
- I8 (5 方法覆盖): step_into/over/out/run_until/next_hle all use broadcast.
- I9 (API 兼容): interval_ms param still accepted (deprecated).
- I10 (reason 不丢): reason field preserved (legacy lost this).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


def _push_stepping_broadcast(
    transport: Any,
    reason: str = "cpu.stepInto",
    pc: int = 0x08804000,
) -> None:
    """Push a cpu.stepping broadcast onto the transport's events queue."""
    transport.push_broadcast(
        {
            "event": "cpu.stepping",
            "pc": pc,
            "ticks": 12345.0,
            "reason": reason,
            "relatedAddress": 0,
        }
    )


class TestV004ConfirmStepBroadcast:
    """V004 broadcast path: _confirm_step_completed subscribes to cpu.stepping.

    Default `use_broadcast=True` — these tests anchor the broadcast path
    that became the default in stage 5 (B.2 spec §2.4).
    """

    @pytest.mark.asyncio
    async def test_confirm_step_uses_wait_for_broadcast_by_default(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """I8 anchor: default path subscribes to cpu.stepping broadcast."""
        transport.wait_for_broadcast = AsyncMock(
            return_value={
                "event": "cpu.stepping",
                "pc": 0x08804000,
                "ticks": 1.0,
                "reason": "cpu.stepInto",
                "relatedAddress": 0,
            }
        )
        await client._confirm_step_completed(timeout_ms=1000)
        assert transport.wait_for_broadcast.await_count == 1, (
            "_confirm_step_completed must call wait_for_broadcast by "
            "default (V004 stage 5 redesign). If this fails, the "
            "default was changed back to wait_for_state."
        )
        # Verify event name is "cpu.stepping"
        call_args = transport.wait_for_broadcast.await_args
        assert call_args.kwargs.get("event") == "cpu.stepping", (
            "wait_for_broadcast must subscribe to 'cpu.stepping' — see "
            "SteppingBroadcaster.cpp:L57-72."
        )

    @pytest.mark.asyncio
    async def test_confirm_step_returns_full_broadcast_dict(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """I6 anchor: returned dict has event/pc/ticks/reason/relatedAddress."""
        broadcast = {
            "event": "cpu.stepping",
            "pc": 0x08804000,
            "ticks": 12345.0,
            "reason": "breakpoint",
            "relatedAddress": 0xDEADBEEF,
        }
        transport.wait_for_broadcast = AsyncMock(return_value=broadcast)
        result = await client._confirm_step_completed(timeout_ms=500)
        assert result == broadcast, (
            "_confirm_step_completed must return the full cpu.stepping "
            "broadcast dict (5 fields: event/pc/ticks/reason/relatedAddress)."
        )
        # Verify all 5 required fields are present.
        for field in ("event", "pc", "ticks", "reason", "relatedAddress"):
            assert field in result, (
                f"missing field '{field}' in broadcast dict — see SteppingBroadcaster.cpp:L25-44."
            )

    @pytest.mark.asyncio
    async def test_confirm_step_preserves_reason_field(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """I10 anchor: reason field is preserved (legacy path lost this)."""
        # The legacy wait_for_state path only returned {stepping: bool},
        # losing the reason context. The broadcast path preserves it.
        transport.wait_for_broadcast = AsyncMock(
            return_value={
                "event": "cpu.stepping",
                "pc": 0x08804000,
                "ticks": 1.0,
                "reason": "breakpoint",
                "relatedAddress": 0,
            }
        )
        result = await client._confirm_step_completed(timeout_ms=500)
        assert result["reason"] == "breakpoint", (
            "reason field must be preserved from the cpu.stepping "
            "broadcast — legacy path lost this. See SteppingBroadcaster."
            "cpp:L36 for reason field."
        )

    @pytest.mark.asyncio
    async def test_confirm_step_default_timeout_5000(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """I7 anchor: default timeout_ms forwarded unchanged (5000ms)."""
        transport.wait_for_broadcast = AsyncMock(
            return_value={
                "event": "cpu.stepping",
                "pc": 0,
                "ticks": 0.0,
                "reason": "cpu.stepInto",
                "relatedAddress": 0,
            }
        )
        await client._confirm_step_completed()
        forwarded_timeout = transport.wait_for_broadcast.await_args.kwargs.get("timeout_ms")
        assert forwarded_timeout == 5000, (
            f"wait_for_broadcast must receive default timeout_ms=5000, got {forwarded_timeout!r}."
        )

    @pytest.mark.asyncio
    async def test_confirm_step_interval_ms_accepted_for_compat(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """I9 anchor: interval_ms param still accepted (deprecated, no-op)."""
        transport.wait_for_broadcast = AsyncMock(
            return_value={
                "event": "cpu.stepping",
                "pc": 0,
                "ticks": 0.0,
                "reason": "cpu.stepInto",
                "relatedAddress": 0,
            }
        )
        # Should not raise — interval_ms is accepted for API compat.
        await client._confirm_step_completed(timeout_ms=500, interval_ms=50)
        assert transport.wait_for_broadcast.await_count == 1

    @pytest.mark.asyncio
    async def test_confirm_step_broadcast_timeout_falls_back_to_legacy(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """Degraded fallback: broadcast TimeoutError → legacy wait_for_state.

        Old PPSSPP builds may not push `cpu.stepping` broadcasts. The
        B.2 spec §2.4.3 design requires the broadcast path to fall back
        to the legacy `wait_for_state` poll when broadcast times out.
        """
        transport.wait_for_broadcast = AsyncMock(side_effect=TimeoutError("broadcast timeout"))
        transport.wait_for_state = AsyncMock(return_value={"stepping": True})
        await client._confirm_step_completed(timeout_ms=100, interval_ms=10)
        # Both paths must be tried: broadcast first, then legacy fallback.
        assert transport.wait_for_broadcast.await_count == 1, (
            "Broadcast mode must be attempted first."
        )
        assert transport.wait_for_state.await_count == 1, (
            "Legacy fallback must be invoked when broadcast times out "
            "(B.2 spec §2.4.3 degraded path)."
        )

    @pytest.mark.asyncio
    async def test_confirm_step_no_legacy_when_broadcast_succeeds(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """Broadcast success must NOT invoke legacy wait_for_state."""
        transport.wait_for_broadcast = AsyncMock(
            return_value={
                "event": "cpu.stepping",
                "pc": 0x08804000,
                "ticks": 0.0,
                "reason": "cpu.stepInto",
                "relatedAddress": 0,
            }
        )
        transport.wait_for_state = AsyncMock(return_value={"stepping": True})
        await client._confirm_step_completed(timeout_ms=500)
        assert transport.wait_for_broadcast.await_count == 1
        assert transport.wait_for_state.await_count == 0, (
            "Legacy wait_for_state must NOT be called when broadcast "
            "succeeds — only the timeout path triggers fallback."
        )


class TestV004AllStepMethodsUseBroadcast:
    """I8 anchor: all 5 step methods use the broadcast path by default.

    Each step method delegates to `_confirm_step_completed` which uses
    `wait_for_broadcast("cpu.stepping")` by default. Verifying all 5
    methods reach the broadcast path ensures no method was missed.
    """

    @pytest.mark.asyncio
    async def test_step_into_uses_broadcast(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        transport.wait_for_broadcast = AsyncMock(
            return_value={
                "event": "cpu.stepping",
                "pc": 0x08804000,
                "ticks": 0.0,
                "reason": "cpu.stepInto",
                "relatedAddress": 0,
            }
        )
        await client.step_into(timeout_ms=500)
        assert transport.wait_for_broadcast.await_count == 1
        assert transport.fire_and_forget_calls[-1][0] == "cpu.stepInto"

    @pytest.mark.asyncio
    async def test_step_over_uses_broadcast(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        transport.wait_for_broadcast = AsyncMock(
            return_value={
                "event": "cpu.stepping",
                "pc": 0x08804000,
                "ticks": 0.0,
                "reason": "cpu.stepOver",
                "relatedAddress": 0,
            }
        )
        await client.step_over(timeout_ms=500)
        assert transport.wait_for_broadcast.await_count == 1
        assert transport.fire_and_forget_calls[-1][0] == "cpu.stepOver"

    @pytest.mark.asyncio
    async def test_step_out_uses_broadcast(self, client: PpssppDebugClient, transport: Any) -> None:
        transport.wait_for_broadcast = AsyncMock(
            return_value={
                "event": "cpu.stepping",
                # A2: pc must be inside PSP executable code range
                # (0x08800000–0x0C000000) — step_out post-validates.
                "pc": 0x08804000,
                "ticks": 0.0,
                "reason": "cpu.stepOut",
                "relatedAddress": 0,
            }
        )
        await client.step_out(timeout_ms=500)
        assert transport.wait_for_broadcast.await_count == 1
        assert transport.fire_and_forget_calls[-1][0] == "cpu.stepOut"

    @pytest.mark.asyncio
    async def test_run_until_uses_broadcast(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        transport.wait_for_broadcast = AsyncMock(
            return_value={
                "event": "cpu.stepping",
                "pc": 0x08804000,
                "ticks": 0.0,
                "reason": "breakpoint",
                "relatedAddress": 0x08804000,
            }
        )
        await client.run_until(0x08804000, timeout_ms=500)
        assert transport.wait_for_broadcast.await_count == 1
        assert transport.fire_and_forget_calls[-1][0] == "cpu.runUntil"
        assert transport.fire_and_forget_calls[-1][1] == {"address": 0x08804000}

    @pytest.mark.asyncio
    async def test_next_hle_uses_broadcast(self, client: PpssppDebugClient, transport: Any) -> None:
        transport.wait_for_broadcast = AsyncMock(
            return_value={
                "event": "cpu.stepping",
                "pc": 0x08804000,
                "ticks": 0.0,
                "reason": "cpu.nextHLE",
                "relatedAddress": 0,
            }
        )
        await client.next_hle(timeout_ms=500)
        assert transport.wait_for_broadcast.await_count == 1
        assert transport.fire_and_forget_calls[-1][0] == "cpu.nextHLE"


class TestV004WaitForBroadcastInvariants:
    """I1-I5 anchors for transport.wait_for_broadcast itself.

    These verify the drain-and-requeue pattern: non-matching messages
    are accumulated in a backlog and requeued before return.
    """

    @pytest.mark.asyncio
    async def test_no_message_loss_on_success(self, transport: Any) -> None:
        """I1 anchor: non-matching messages are requeued after success."""
        # Push 1 non-matching + 1 matching broadcast.
        transport.push_broadcast({"event": "other", "data": 1})
        transport.push_broadcast({"event": "cpu.stepping", "pc": 0})
        msg = await transport.wait_for_broadcast("cpu.stepping", timeout_ms=500)
        assert msg["event"] == "cpu.stepping"
        # The non-matching message must still be in the queue.
        assert transport.events.qsize() == 1
        leftover = transport.events.get_nowait()
        assert leftover["event"] == "other"

    @pytest.mark.asyncio
    async def test_timeout_preserves_backlog(self, transport: Any) -> None:
        """I2 anchor: timeout requeues all backlog messages in FIFO order."""
        transport.push_broadcast({"event": "other", "data": "A"})
        transport.push_broadcast({"event": "other", "data": "B"})
        transport.push_broadcast({"event": "other", "data": "C"})
        with pytest.raises(TimeoutError):
            await transport.wait_for_broadcast("cpu.stepping", timeout_ms=50)
        # All 3 non-matching messages must be requeued in order.
        assert transport.events.qsize() == 3
        a = transport.events.get_nowait()
        b = transport.events.get_nowait()
        c = transport.events.get_nowait()
        assert (a["data"], b["data"], c["data"]) == ("A", "B", "C")

    @pytest.mark.asyncio
    async def test_filter_none_matches_all(self, transport: Any) -> None:
        """I3 anchor: filter=None matches all messages with event == target."""
        transport.push_broadcast({"event": "cpu.stepping", "pc": 1})
        transport.push_broadcast({"event": "cpu.stepping", "pc": 2})
        msg = await transport.wait_for_broadcast("cpu.stepping", timeout_ms=500)
        assert msg["pc"] == 1, "filter=None returns the first match"
        # The second match must still be in the queue.
        assert transport.events.qsize() == 1

    @pytest.mark.asyncio
    async def test_filter_predicate_selects_match(self, transport: Any) -> None:
        """I3 anchor: filter predicate selects among same-event broadcasts."""
        transport.push_broadcast({"event": "cpu.stepping", "reason": "stepInto"})
        transport.push_broadcast({"event": "cpu.stepping", "reason": "breakpoint"})
        msg = await transport.wait_for_broadcast(
            "cpu.stepping",
            timeout_ms=500,
            filter=lambda m: m["reason"] == "breakpoint",
        )
        assert msg["reason"] == "breakpoint"
        # The non-matching same-event message must be requeued.
        assert transport.events.qsize() == 1
        leftover = transport.events.get_nowait()
        assert leftover["reason"] == "stepInto"

    @pytest.mark.asyncio
    async def test_backlog_order_preserved_on_timeout(self, transport: Any) -> None:
        """I5 anchor: requeued backlog preserves original FIFO order."""
        msgs = [{"event": "other", "i": i} for i in range(5)]
        for m in msgs:
            transport.push_broadcast(m)
        with pytest.raises(TimeoutError):
            await transport.wait_for_broadcast("cpu.stepping", timeout_ms=50)
        requeued = []
        while not transport.events.empty():
            requeued.append(transport.events.get_nowait())
        assert [m["i"] for m in requeued] == [0, 1, 2, 3, 4]
