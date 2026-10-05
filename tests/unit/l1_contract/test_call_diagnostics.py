"""Ticketed-call diagnostics wired into the transport (T007 / FR-025 / C4.3).

D16 needed data, not a guess: `gpu.stats.get` times out on a long-running
session while the game renders at 60 fps, and the tool blamed loading.

The record's field set and its three outcomes are pinned by
`tests/unit/service/test_diagnostic_record.py`. What remains here is the
wiring those tests cannot see: that `WsTransport.call()` actually records
into `diagnostics`, and that an unresponsive debugger does not recurse.

Falsifiable: every assertion below fails if the corresponding field is
dropped, renamed, or left at its default.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ppsspp_dfx_mcp.core.transport import WsTransport


class TestTransportIntegration:
    """The record must be wired into WsTransport.call(), not just defined."""

    @staticmethod
    def _connected_transport(respond: bool = True):
        """A WsTransport whose `ws` passes the real State.OPEN check.

        Uses the genuine websockets State enum (same trick as
        tests/integration/test_game_state_observer_integration.py) so the
        `self.ws.state == State.OPEN` guard is genuinely satisfied instead
        of stubbed around.
        """
        from websockets.protocol import State as WsState

        from ppsspp_dfx_mcp.core.transport import WsTransport

        t = WsTransport("127.0.0.1", 1)
        sock = _FakeSocket(respond=respond)
        sock.bind(t)
        sock.state = WsState.OPEN
        t.ws = sock  # type: ignore[assignment]
        return t

    @pytest.mark.asyncio
    async def test_call_records_success(self) -> None:
        t = self._connected_transport()
        out = await t.call("gpu.stats.get", timeout=1.0)
        assert out["fps"] == 60
        rec = t.diagnostics.last("gpu.stats.get")
        assert rec is not None, "call() did not record anything"
        assert rec["settled"] is True
        assert rec["timed_out"] is False
        assert rec["elapsed_s"] is not None

    @pytest.mark.asyncio
    async def test_call_records_timeout(self) -> None:
        t = self._connected_transport(respond=False)
        # The cpu.status probe inside settle() is itself a call(); it times
        # out too, which _probe_stepping_best_effort swallows -> None.
        with pytest.raises(TimeoutError):
            await t.call("gpu.stats.get", timeout=0.2)
        rec = t.diagnostics.last("gpu.stats.get")
        assert rec is not None
        assert rec["timed_out"] is True
        assert rec["elapsed_s"] is not None

    @pytest.mark.asyncio
    async def test_timeout_records_unknown_stepping_without_raising(self) -> None:
        """stepping_at_timeout must be None (not False) when unprobeable."""
        t = self._connected_transport(respond=False)
        with pytest.raises(TimeoutError):
            await t.call("gpu.stats.get", timeout=0.2)
        rec = t.diagnostics.last("gpu.stats.get")
        assert rec["stepping_at_timeout"] is None

    @pytest.mark.asyncio
    async def test_unresponsive_debugger_does_not_recurse_forever(self) -> None:
        """Regression: the stepping probe issues a call() of its own.

        A call() that times out probes stepping, which issues another
        call(), which times out... Without the re-entrancy latch this
        recursed until the event loop stalled. The call must return (or
        raise) promptly instead.
        """
        t = self._connected_transport(respond=False)
        loop = asyncio.get_running_loop()
        start = loop.time()
        with pytest.raises(TimeoutError):
            await t.call("gpu.stats.get", timeout=0.2)
        elapsed = loop.time() - start
        # One 0.2s timeout plus at most one guarded probe; unbounded
        # recursion would never return at all.
        assert elapsed < 5.0, f"call took {elapsed:.1f}s — probe likely recursing"
        # The probe gave up rather than nesting further calls.
        assert t._probing_stepping is False


class _FakeSocket:
    """Minimal socket stand-in that answers one ticketed request."""

    def __init__(self, respond: bool = True) -> None:
        self.state = None
        self._transport: WsTransport | None = None
        self._respond = respond

    def bind(self, transport: WsTransport) -> None:
        self._transport = transport

    async def send(self, raw: str) -> None:
        if not self._respond or self._transport is None:
            return
        import json as _json

        msg = _json.loads(raw)
        ticket = msg.get("ticket")
        fut = self._transport._pending.get(ticket)
        if fut is not None and not fut.done():
            fut.set_result({"event": msg.get("event"), "fps": 60})
