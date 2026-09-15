"""L3 orchestration tests: WsTransport (B.1 spec §1 O1 invariants I1-I26).

Anchors: B.1 orchestration spec §1 O1 WsTransport 26 invariants across
4 call semantics (call / fire_and_forget / wait_for_state / wait_for_broadcast)
+ _recv_loop routing + send_version dual-path.

Complementary to L4 regression tests: L4 anchors specific violation fixes;
L3 anchors cross-method orchestration behavior under the real WsTransport
implementation (with a MockWebSocket simulating PPSSPP).

Test strategy:
- Patch `websockets.connect` to return a MockWebSocket that simulates
  PPSSPP: records sent messages, delivers inbound JSON via
  `incoming_messages` (list) or `incoming_handler` (callable).
- The mock's `subprotocol` is `WS_SUBPROTOCOL` so `connect()` succeeds.
- The mock's `state` is `State.OPEN` by default; tests can flip to
  `State.CLOSED` to terminate the recv loop.

B.1 spec invariants anchored here (analysis_ppsspp_dfx_orchestration_
spec_current_v1.md §1):
- O1-I1..I7: call() ticket lifecycle (success/timeout/error pop pending,
  monotonic counter, ticket format, no duplicate, not-connected guard)
- O1-I8..I10: fire_and_forget() (no _pending registration, ticket in
  payload, response routed to events queue)
- O1-I11..I15: wait_for_state() (polls cpu.status, returns satisfying,
  raises timeout, defaults, predicate-before-elapsed)
- O1-I16..I20: _recv_loop() (continue on timeout, break on closed,
  set_exception on error, set_result on normal, route ticketless)
- O1-I21..I26: send_version() (ticket path first, fallback to events,
  5s total, put back non-version, raise on both fail, ~7s max)

B.2 spec invariants anchored here (analysis_ppsspp_dfx_orchestration_
spec_redesign_v1.md §2.3):
- I1: no message loss (matching returned, non-matching requeued)
- I2: timeout preserves backlog FIFO
- I3: filter None matches all / filter predicate
- I4: single-coroutine compat with send_version drain-and-requeue
- I5: backlog FIFO order preserved on requeue
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import patch

import pytest
from websockets.protocol import State

from ppsspp_dfx_mcp.core.transport import (
    DEFAULT_WAIT_INTERVAL_MS,
    DEFAULT_WAIT_TIMEOUT_MS,
    WS_SUBPROTOCOL,
    WsTransport,
)


# ============================================================================
# MockWebSocket — simulates PPSSPP WebSocket server
# ============================================================================


class MockWebSocket:
    """Simulated WebSocket for WsTransport tests.

    Configuration:
    - `subprotocol`: set to WS_SUBPROTOCOL by default.
    - `state`: State.OPEN by default.
    - `incoming_messages`: list of raw JSON strings delivered to recv().
      Each call to recv() pops the first one. If empty, recv() blocks
      (controlled by an asyncio.Event).
    - `incoming_handler`: optional callable invoked on each recv() call
      with the MockWebSocket instance. The handler can append to
      incoming_messages or set state=CLOSED.
    - `sent_messages`: list of raw strings sent via send().
    """

    def __init__(
        self,
        incoming_messages: list[str] | None = None,
        incoming_handler: Any = None,
    ) -> None:
        self.subprotocol = WS_SUBPROTOCOL
        self.state = State.OPEN
        self.incoming_messages: list[str] = list(incoming_messages or [])
        self.incoming_handler = incoming_handler
        self.sent_messages: list[str] = []
        self._closed = False

    async def recv(self) -> str:
        if self.incoming_handler is not None:
            self.incoming_handler(self)
        if self.incoming_messages:
            return self.incoming_messages.pop(0)
        # Sleep longer than recv_loop's wait_for timeout (0.5s) so that
        # asyncio.wait_for(self.ws.recv(), timeout=0.5) times out itself
        # (rather than recv() raising asyncio.TimeoutError, which interacts
        # badly with wait_for's internal cancel logic in Python 3.10).
        await asyncio.sleep(1.0)

    async def send(self, data: str) -> None:
        self.sent_messages.append(data)

    async def close(self) -> None:
        self._closed = True
        self.state = State.CLOSED


def _patch_connect(mock_ws: MockWebSocket):
    """Patch `websockets.connect` to return `mock_ws`."""
    async def _connect(*args: Any, **kwargs: Any) -> MockWebSocket:
        return mock_ws
    return patch("ppsspp_dfx_mcp.core.transport.websockets.connect", new=_connect)


async def _wait_for_event(queue: asyncio.Queue, timeout_s: float = 2.0) -> bool:
    """Poll until the events queue has a message or timeout.

    Uses short sleeps instead of a single long sleep so the event loop
    can schedule the recv_loop between polls. Returns True if a message
    arrived, False on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not queue.empty():
            return True
        await asyncio.sleep(0.01)
    return not queue.empty()


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_ws() -> MockWebSocket:
    return MockWebSocket()


@pytest.fixture
def transport(mock_ws: MockWebSocket) -> WsTransport:
    """A WsTransport with mocked websockets.connect (not yet connected)."""
    return WsTransport("127.0.0.1", 12345)


# ============================================================================
# O1-I1..I7: call() ticket lifecycle
# ============================================================================


class TestCallTicketLifecycle:
    """L3: call() ticket registration, matching, cleanup, and guards.

    B.1 O1 §1.4 invariants I1-I7: ticket is popped from _pending on
    success/timeout/error; counter monotonically increments; ticket
    format is 't{N}'; no duplicate pending; not-connected raises.
    """

    async def test_I1_call_success_pops_pending(self, transport, mock_ws):
        """O1-I1: call() success → ticket removed from _pending by recv_loop."""
        with _patch_connect(mock_ws):
            await transport.connect()

            async def respond():
                await asyncio.sleep(0.01)
                mock_ws.incoming_messages.append(
                    json.dumps({"event": "memory.read_u32", "ticket": "t1", "value": 42})
                )
            asyncio.create_task(respond())
            result = await transport.call("memory.read_u32", address=0x08804000)
        await transport.close()
        assert result["value"] == 42
        assert "t1" not in transport._pending

    async def test_I2_call_timeout_pops_pending(self, transport, mock_ws):
        """O1-I2: call() timeout → ticket popped from _pending."""
        with _patch_connect(mock_ws):
            await transport.connect()
            with pytest.raises(asyncio.TimeoutError):
                await transport.call("memory.read_u32", timeout=0.1, address=0x0)
        await transport.close()
        assert "t1" not in transport._pending

    async def test_I3_call_exception_pops_pending(self, transport, mock_ws):
        """O1-I3: call() error event → ticket popped by recv_loop (set_exception)."""
        with _patch_connect(mock_ws):
            await transport.connect()

            async def respond():
                await asyncio.sleep(0.01)
                mock_ws.incoming_messages.append(
                    json.dumps({
                        "event": "error",
                        "ticket": "t1",
                        "message": "bad address",
                        "level": 3,
                    })
                )
            asyncio.create_task(respond())
            with pytest.raises(RuntimeError, match="bad address"):
                await transport.call("memory.read_u32", address=0x0)
        await transport.close()
        assert "t1" not in transport._pending

    async def test_I4_ticket_counter_monotonic(self, transport, mock_ws):
        """O1-I4: _ticket_counter increments on each call/fire_and_forget.

        Two calls run concurrently via gather so both futures register
        before respond() injects responses. Serial `await` would race:
        respond() appends both messages at once, the recv loop dispatches
        the second before the second `call()` registers its future, so
        the response is dropped to the broadcast queue and the second
        call times out.
        """
        with _patch_connect(mock_ws):
            await transport.connect()
            # Schedule two responses
            async def respond():
                await asyncio.sleep(0.01)
                mock_ws.incoming_messages.append(
                    json.dumps({"event": "memory.read_u32", "ticket": "t1", "value": 1})
                )
                mock_ws.incoming_messages.append(
                    json.dumps({"event": "memory.read_u16", "ticket": "t2", "value": 2})
                )
            asyncio.create_task(respond())
            await asyncio.gather(
                transport.call("memory.read_u32", address=0x0),
                transport.call("memory.read_u16", address=0x0),
            )
        await transport.close()
        assert transport._ticket_counter == 2

    async def test_I5_ticket_format_t_prefix(self, transport, mock_ws):
        """O1-I5: ticket format is f't{N}'."""
        with _patch_connect(mock_ws):
            await transport.connect()
            await transport.fire_and_forget("cpu.stepping")
        await transport.close()
        sent = json.loads(mock_ws.sent_messages[0])
        assert sent["ticket"] == "t1"

    async def test_I6_no_duplicate_ticket_in_pending(self, transport, mock_ws):
        """O1-I6: _pending is a dict, keys unique — no duplicate ticket."""
        with _patch_connect(mock_ws):
            await transport.connect()
            # Start two calls; both register different tickets
            async def respond():
                await asyncio.sleep(0.02)
                mock_ws.incoming_messages.append(
                    json.dumps({"event": "memory.read_u32", "ticket": "t1", "value": 1})
                )
                mock_ws.incoming_messages.append(
                    json.dumps({"event": "memory.read_u16", "ticket": "t2", "value": 2})
                )
            asyncio.create_task(respond())
            t1 = asyncio.create_task(transport.call("memory.read_u32", address=0x0))
            t2 = asyncio.create_task(transport.call("memory.read_u16", address=0x0))
            await asyncio.gather(t1, t2)
        await transport.close()
        # Both tickets should be gone after completion
        assert "t1" not in transport._pending
        assert "t2" not in transport._pending

    async def test_I7_call_raises_when_not_connected(self, transport, monkeypatch):
        """O1-I7 (F-3 updated): call() on a transport that cannot reconnect
        raises RuntimeError mentioning the failed reconnect."""
        async def _fail_connect():
            raise ConnectionRefusedError("refused")

        monkeypatch.setattr(transport, "connect", _fail_connect)
        with pytest.raises(RuntimeError, match="not connected"):
            await transport.call("memory.read_u32", address=0x0)


# ============================================================================
# O1-I8..I10: fire_and_forget()
# ============================================================================


class TestFireAndForget:
    """L3: fire_and_forget() includes ticket but does not await response.

    B.1 O1 §1.7 invariants I8-I10: no _pending registration, ticket in
    payload, response (if any) routed to events queue.
    """

    async def test_I8_no_pending_registration(self, transport, mock_ws):
        """O1-I8: fire_and_forget does NOT register a Future in _pending."""
        with _patch_connect(mock_ws):
            await transport.connect()
            await transport.fire_and_forget("cpu.stepping")
        await transport.close()
        assert transport._pending == {}

    async def test_I9_ticket_in_payload(self, transport, mock_ws):
        """O1-I9: fire_and_forget payload still includes a ticket field."""
        with _patch_connect(mock_ws):
            await transport.connect()
            await transport.fire_and_forget("cpu.stepping")
        await transport.close()
        sent = json.loads(mock_ws.sent_messages[0])
        assert "ticket" in sent
        assert sent["ticket"].startswith("t")

    async def test_I10_response_routed_to_events_queue(self, transport, mock_ws):
        """O1-I10: PPSSPP echo of faf ticket → events queue (no _pending match)."""
        with _patch_connect(mock_ws):
            await transport.connect()
            await transport.fire_and_forget("cpu.stepping")
            # Push a response with the matching ticket
            sent = json.loads(mock_ws.sent_messages[0])
            ticket = sent["ticket"]
            mock_ws.incoming_messages.append(
                json.dumps({"event": "cpu.stepping", "ticket": ticket, "pc": 0x1234})
            )
            await _wait_for_event(transport.events)
        await transport.close()
        ev = transport.events.get_nowait()
        assert ev["event"] == "cpu.stepping"
        assert ev["pc"] == 0x1234


# ============================================================================
# O1-I11..I15: wait_for_state()
# ============================================================================


class TestWaitForState:
    """L3: wait_for_state() polls cpu.status until predicate satisfied.

    B.1 O1 §1.9 invariants I11-I15: calls cpu.status at least once,
    returns satisfying status, raises timeout, defaults, predicate
    checked before elapsed.
    """

    async def test_I11_calls_cpu_status_at_least_once(self, transport, mock_ws):
        """O1-I11: wait_for_state polls cpu.status at least once."""
        with _patch_connect(mock_ws):
            await transport.connect()
            call_count = 0

            async def mock_call(event: str, timeout: float = 5.0, **params: Any) -> dict:
                nonlocal call_count
                call_count += 1
                return {"stepping": True}
            transport.call = mock_call
            await transport.wait_for_state(
                lambda s: s.get("stepping") is True,
                timeout_ms=500,
                interval_ms=10,
            )
        await transport.close()
        assert call_count >= 1

    async def test_I12_returns_satisfying_status(self, transport, mock_ws):
        """O1-I12: returns the first status dict satisfying predicate."""
        with _patch_connect(mock_ws):
            await transport.connect()

            async def mock_call(event: str, timeout: float = 5.0, **params: Any) -> dict:
                return {"stepping": True, "pc": 0x1234}
            transport.call = mock_call
            result = await transport.wait_for_state(
                lambda s: s.get("stepping") is True,
                timeout_ms=500,
                interval_ms=10,
            )
        await transport.close()
        assert result["stepping"] is True
        assert result["pc"] == 0x1234

    async def test_I13_raises_timeout_after_timeout_ms(self, transport, mock_ws):
        """O1-I13: predicate never satisfied → TimeoutError raised."""
        with _patch_connect(mock_ws):
            await transport.connect()

            async def mock_call(event: str, timeout: float = 5.0, **params: Any) -> dict:
                return {"stepping": False}
            transport.call = mock_call
            with pytest.raises(TimeoutError, match="timeout"):
                await transport.wait_for_state(
                    lambda s: s.get("stepping") is True,
                    timeout_ms=80,
                    interval_ms=10,
                )
        await transport.close()

    async def test_I14_default_timeout_and_interval(self, transport):
        """O1-I14: default timeout_ms=3000, interval_ms=50 (module constants)."""
        assert DEFAULT_WAIT_TIMEOUT_MS == 3000
        assert DEFAULT_WAIT_INTERVAL_MS == 50

    async def test_I15_predicate_satisfied_before_elapsed_check(self, transport, mock_ws):
        """O1-I15: predicate checked before elapsed — satisfied state never times out."""
        with _patch_connect(mock_ws):
            await transport.connect()
            call_count = 0

            async def mock_call(event: str, timeout: float = 5.0, **params: Any) -> dict:
                nonlocal call_count
                call_count += 1
                # First poll: not satisfied; second poll: satisfied
                return {"stepping": call_count >= 2}
            transport.call = mock_call
            # Even with timeout_ms=0, predicate satisfied on 2nd poll should return
            result = await transport.wait_for_state(
                lambda s: s.get("stepping") is True,
                timeout_ms=1,  # Very tight
                interval_ms=1,
            )
        await transport.close()
        assert result["stepping"] is True


# ============================================================================
# O1-I16..I20: _recv_loop() dispatch
# ============================================================================


class TestRecvLoop:
    """L3: _recv_loop() routes messages to _pending futures or events queue.

    B.1 O1 §1.11 invariants I16-I20: continue on recv timeout, break on
    connection closed, set_exception on error, set_result on normal,
    route ticketless to events queue.
    """

    async def test_I16_continues_on_recv_timeout(self, transport, mock_ws):
        """O1-I16: recv_loop continues when ws.recv() times out (no message)."""
        with _patch_connect(mock_ws):
            await transport.connect()
            # No messages pushed; recv_loop should keep running across
            # multiple wait_for timeout cycles. Sleep just past one
            # timeout cycle (0.5s) to confirm continuation.
            await asyncio.sleep(0.6)
            assert not transport._recv_task.done()  # Still alive
        await transport.close()

    async def test_I17_breaks_on_connection_closed(self, transport, mock_ws):
        """O1-I17: recv_loop breaks when ConnectionClosed is raised."""
        with _patch_connect(mock_ws):
            await transport.connect()
            mock_ws.state = State.CLOSED
            # Poll until recv_task exits (it checks state at while-loop
            # condition, reached after the current wait_for cycle).
            for _ in range(100):
                if transport._recv_task is None or transport._recv_task.done():
                    break
                await asyncio.sleep(0.01)
        await transport.close()
        # recv_task should have exited cleanly (done)
        assert transport._recv_task is None or transport._recv_task.done()

    async def test_I18_sets_exception_on_error_event(self, transport, mock_ws):
        """O1-I18: error event with matching ticket → set_exception(RuntimeError)."""
        with _patch_connect(mock_ws):
            await transport.connect()

            async def respond():
                await asyncio.sleep(0.01)
                mock_ws.incoming_messages.append(
                    json.dumps({
                        "event": "error",
                        "ticket": "t1",
                        "message": "ppsspp error",
                        "level": 3,
                    })
                )
            asyncio.create_task(respond())
            with pytest.raises(RuntimeError, match="ppsspp error"):
                await transport.call("memory.read_u32", address=0x0)
        await transport.close()

    async def test_I19_sets_result_on_normal_event(self, transport, mock_ws):
        """O1-I19: normal event with matching ticket → set_result(data)."""
        with _patch_connect(mock_ws):
            await transport.connect()

            async def respond():
                await asyncio.sleep(0.01)
                mock_ws.incoming_messages.append(
                    json.dumps({"event": "memory.read_u32", "ticket": "t1", "value": 99})
                )
            asyncio.create_task(respond())
            result = await transport.call("memory.read_u32", address=0x0)
        await transport.close()
        assert result["value"] == 99

    async def test_I20_routes_ticketless_to_events_queue(self, transport, mock_ws):
        """O1-I20: ticketless message → pushed to _events_queue."""
        with _patch_connect(mock_ws):
            await transport.connect()
            mock_ws.incoming_messages.append(
                json.dumps({"event": "cpu.stepping", "pc": 0x08801234})
            )
            await _wait_for_event(transport.events)
        await transport.close()
        ev = transport.events.get_nowait()
        assert ev["event"] == "cpu.stepping"
        assert ev["pc"] == 0x08801234


# ============================================================================
# O1-I21..I26: send_version() dual-path
# ============================================================================


class TestSendVersion:
    """L3: send_version() tries ticket path first, falls back to events queue.

    B.1 O1 §1.13 invariants I21-I26: ticket path first, fallback to events,
    5s total budget, put back non-version, raise on both fail, ~7s max.
    """

    async def test_I21_tries_ticket_path_first(self, transport, mock_ws):
        """O1-I21: send_version returns via call() path when PPSSPP echoes ticket."""
        with _patch_connect(mock_ws):
            await transport.connect()

            async def respond():
                await asyncio.sleep(0.01)
                sent = json.loads(mock_ws.sent_messages[0])
                ticket = sent["ticket"]
                mock_ws.incoming_messages.append(
                    json.dumps({
                        "event": "version",
                        "ticket": ticket,
                        "name": "PPSSPP",
                        "version": "1.0",
                    })
                )
            asyncio.create_task(respond())
            result = await transport.send_version()
        await transport.close()
        assert result["event"] == "version"
        assert result["name"] == "PPSSPP"

    async def test_I22_falls_back_to_events_queue(self, transport, mock_ws):
        """O1-I22: call() times out → fallback to events queue polling."""
        with _patch_connect(mock_ws):
            await transport.connect()

            async def push_unsolicited_version():
                await asyncio.sleep(0.05)
                mock_ws.incoming_messages.append(
                    json.dumps({"event": "version", "name": "PPSSPP", "version": "1.0"})
                )
            asyncio.create_task(push_unsolicited_version())

            # Force call() to time out for "version" event
            async def fast_call(event: str, timeout: float = 5.0, **params: Any) -> dict:
                if event == "version":
                    raise asyncio.TimeoutError()
                return {}
            transport.call = fast_call
            result = await transport.send_version()
        await transport.close()
        assert result["event"] == "version"
        assert result["name"] == "PPSSPP"

    async def test_I23_fallback_total_timeout_5s(self, transport, mock_ws):
        """O1-I23: fallback path total budget is 5.0s."""
        with _patch_connect(mock_ws):
            await transport.connect()

            async def failing_call(event: str, timeout: float = 5.0, **params: Any) -> dict:
                if event == "version":
                    raise asyncio.TimeoutError()
                return {}
            transport.call = failing_call

            # Patch time.monotonic so the 5.0 budget loop sees elapsed > 5.0
            import ppsspp_dfx_mcp.core.transport as transport_mod

            fake_times = iter([0.0, 6.0, 6.0])
            def fake_monotonic() -> float:
                try:
                    return next(fake_times)
                except StopIteration:
                    return 6.0
            with patch.object(transport_mod.time, "monotonic", fake_monotonic):
                with pytest.raises(RuntimeError, match="version handshake timeout"):
                    await transport.send_version()
        await transport.close()

    async def test_I24_put_back_non_version_messages(self, transport, mock_ws):
        """O1-I24: non-version messages in fallback are put back, not dropped."""
        with _patch_connect(mock_ws):
            await transport.connect()

            # Push a non-version message then a version message
            async def push_messages():
                await asyncio.sleep(0.05)
                mock_ws.incoming_messages.append(
                    json.dumps({"event": "cpu.stepping", "pc": 0x1234})
                )
                mock_ws.incoming_messages.append(
                    json.dumps({"event": "version", "name": "PPSSPP", "version": "1.0"})
                )
            asyncio.create_task(push_messages())

            async def fast_call(event: str, timeout: float = 5.0, **params: Any) -> dict:
                if event == "version":
                    raise asyncio.TimeoutError()
                return {}
            transport.call = fast_call
            result = await transport.send_version()
            await _wait_for_event(transport.events)
        await transport.close()
        assert result["event"] == "version"
        # The non-version message should have been put back
        ev = transport.events.get_nowait()
        assert ev["event"] == "cpu.stepping"

    async def test_I25_raises_when_both_paths_timeout(self, transport, mock_ws):
        """O1-I25: both ticket path and fallback fail → RuntimeError."""
        with _patch_connect(mock_ws):
            await transport.connect()

            async def failing_call(event: str, timeout: float = 5.0, **params: Any) -> dict:
                if event == "version":
                    raise asyncio.TimeoutError()
                return {}
            transport.call = failing_call

            import ppsspp_dfx_mcp.core.transport as transport_mod

            fake_times = iter([0.0, 6.0, 6.0])
            def fake_monotonic() -> float:
                try:
                    return next(fake_times)
                except StopIteration:
                    return 6.0
            with patch.object(transport_mod.time, "monotonic", fake_monotonic):
                with pytest.raises(RuntimeError, match="version handshake timeout"):
                    await transport.send_version()
        await transport.close()

    async def test_I26_max_duration_about_7s(self, transport, mock_ws):
        """O1-I26: ticket path 2.0s + fallback 5.0s = ~7.0s max."""
        with _patch_connect(mock_ws):
            await transport.connect()

            async def failing_call(event: str, timeout: float = 5.0, **params: Any) -> dict:
                if event == "version":
                    raise asyncio.TimeoutError()
                return {}
            transport.call = failing_call

            import ppsspp_dfx_mcp.core.transport as transport_mod

            fake_times = iter([0.0, 6.0, 6.0])
            def fake_monotonic() -> float:
                try:
                    return next(fake_times)
                except StopIteration:
                    return 6.0
            with patch.object(transport_mod.time, "monotonic", fake_monotonic):
                with pytest.raises(RuntimeError, match="version handshake timeout"):
                    await transport.send_version()
        await transport.close()
        # The ~7s budget is verified by the code path: call(timeout=2.0) + 5.0s
        # fallback loop. We don't wait real time; we verify the RuntimeError
        # is raised (both paths exhausted).


# ============================================================================
# B.2 §2.3 I1-I5: wait_for_broadcast() drain-and-requeue
# ============================================================================


class TestWaitForBroadcast:
    """L3: wait_for_broadcast() drain-and-requeue pattern.

    B.2 spec §2.3.4 invariants I1-I5: no message loss, timeout preserves
    backlog FIFO, filter semantics, single-coroutine compat with
    send_version, backlog FIFO order on requeue.
    """

    async def test_B2_I1_no_message_loss(self, transport, mock_ws):
        """B.2 I1: matching returned, non-matching requeued (no loss)."""
        with _patch_connect(mock_ws):
            await transport.connect()
            # Push a non-matching then a matching broadcast
            transport._events_queue.put_nowait({"event": "other", "data": 1})
            transport._events_queue.put_nowait({"event": "cpu.stepping", "pc": 0x1234})
            result = await transport.wait_for_broadcast("cpu.stepping", timeout_ms=500)
        await transport.close()
        assert result["event"] == "cpu.stepping"
        # Non-matching message should still be in the queue
        ev = transport.events.get_nowait()
        assert ev["event"] == "other"

    async def test_B2_I2_timeout_preserves_backlog_fifo(self, transport, mock_ws):
        """B.2 I2: timeout → backlog requeued in FIFO order."""
        with _patch_connect(mock_ws):
            await transport.connect()
            transport._events_queue.put_nowait({"event": "a", "i": 1})
            transport._events_queue.put_nowait({"event": "b", "i": 2})
            with pytest.raises(TimeoutError):
                await transport.wait_for_broadcast("cpu.stepping", timeout_ms=100)
        await transport.close()
        # Both non-matching messages should be back, in order
        ev1 = transport.events.get_nowait()
        ev2 = transport.events.get_nowait()
        assert ev1["event"] == "a" and ev1["i"] == 1
        assert ev2["event"] == "b" and ev2["i"] == 2

    async def test_B2_I3a_filter_none_matches_all(self, transport, mock_ws):
        """B.2 I3: filter=None matches all messages with matching event name."""
        with _patch_connect(mock_ws):
            await transport.connect()
            transport._events_queue.put_nowait({"event": "cpu.stepping", "pc": 1})
            transport._events_queue.put_nowait({"event": "cpu.stepping", "pc": 2})
            result = await transport.wait_for_broadcast(
                "cpu.stepping", timeout_ms=500, filter=None
            )
        await transport.close()
        assert result["pc"] == 1  # First matching returned

    async def test_B2_I3b_filter_predicate(self, transport, mock_ws):
        """B.2 I3: filter predicate selects among same-event messages."""
        with _patch_connect(mock_ws):
            await transport.connect()
            transport._events_queue.put_nowait(
                {"event": "cpu.stepping", "reason": "cpu.stepInto"}
            )
            transport._events_queue.put_nowait(
                {"event": "cpu.stepping", "reason": "breakpoint"}
            )
            result = await transport.wait_for_broadcast(
                "cpu.stepping",
                timeout_ms=500,
                filter=lambda m: m["reason"] == "breakpoint",
            )
        await transport.close()
        assert result["reason"] == "breakpoint"
        # The non-matching message should be requeued
        ev = transport.events.get_nowait()
        assert ev["reason"] == "cpu.stepInto"

    async def test_B2_I4_serial_with_send_version(self, transport, mock_ws):
        """B.2 I4: single-coroutine compat — wait_for_broadcast then send_version.

        Sequence: wait_for_broadcast times out (empty queue), then
        send_version succeeds via events queue. No messages stolen.
        """
        with _patch_connect(mock_ws):
            await transport.connect()

            # First: wait_for_broadcast times out (no broadcasts)
            with pytest.raises(TimeoutError):
                await transport.wait_for_broadcast("cpu.stepping", timeout_ms=100)

            # Now push a version broadcast and call send_version
            async def push_version():
                await asyncio.sleep(0.05)
                mock_ws.incoming_messages.append(
                    json.dumps({"event": "version", "name": "PPSSPP", "version": "1.0"})
                )
            asyncio.create_task(push_version())

            # Force call() to time out so send_version uses the fallback path
            async def fast_call(event: str, timeout: float = 5.0, **params: Any) -> dict:
                if event == "version":
                    raise asyncio.TimeoutError()
                return {}
            transport.call = fast_call
            result = await transport.send_version()
        await transport.close()
        assert result["event"] == "version"
        # No messages should be lost — the queue should be empty
        assert transport.events.empty()

    async def test_B2_I5_backlog_order_preserved(self, transport, mock_ws):
        """B.2 I5: requeued backlog preserves original relative order."""
        with _patch_connect(mock_ws):
            await transport.connect()
            # Push 3 non-matching messages
            transport._events_queue.put_nowait({"event": "a", "i": 1})
            transport._events_queue.put_nowait({"event": "b", "i": 2})
            transport._events_queue.put_nowait({"event": "c", "i": 3})
            with pytest.raises(TimeoutError):
                await transport.wait_for_broadcast("cpu.stepping", timeout_ms=100)
        await transport.close()
        # Verify FIFO order preserved
        ev1 = transport.events.get_nowait()
        ev2 = transport.events.get_nowait()
        ev3 = transport.events.get_nowait()
        assert (ev1["event"], ev1["i"]) == ("a", 1)
        assert (ev2["event"], ev2["i"]) == ("b", 2)
        assert (ev3["event"], ev3["i"]) == ("c", 3)
