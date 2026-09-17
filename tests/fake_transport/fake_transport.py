"""FakeTransport — test double for WsTransport.

Implements the same `call` / `fire_and_forget` / `wait_for_state` /
`wait_for_broadcast` protocol as WsTransport, but without any network
I/O. Used as:

1. A test double in unit tests for SteppingManager, DebugClient, etc.
2. A CLI dry-run backend (no PPSSPP running).
3. A diagnostic-script test harness.

Design principle: FakeTransport lives in the test tree (tests/fake_transport/)
because it is only used by tests. It satisfies the same implicit protocol as
WsTransport — verified by `test_fake_transport.py`.

Configuration model:
- `set_response(event, response_or_callable)` — configures `call()`
  to return a fixed dict, or invoke a callable(**params) to compute
  the response. If no response is configured for an event, returns {}.
- `set_faf_handler(event, handler)` — configures `fire_and_forget()`
  to invoke `handler(transport, **params)` after recording the call.
  This lets tests simulate state transitions (e.g. set stepping=True
  when "cpu.stepping" is fired).
- `set_state(state_dict)` — directly sets the current `cpu.status`
  response. The default state is `{"stepping": False}`.
- `push_broadcast(msg)` — enqueues a ticketless broadcast message on
  the `events` queue (mirrors WsTransport._recv_loop pushing to
  `_events_queue`). Used to test `wait_for_broadcast`.

`wait_for_state()` checks the current state against the predicate and
returns it immediately if satisfied; otherwise polls with the supplied
interval up to the timeout. Because `fire_and_forget()` is synchronous
and updates state immediately, the predicate will match on the first
check in the typical test flow.

`wait_for_broadcast()` mirrors WsTransport.wait_for_broadcast: drains
the `events` queue, matches by event name + optional filter, requeues
non-matching messages in FIFO order before returning.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable
from typing import Any

# Type alias for response configuration: either a fixed dict or a
# callable that takes the call's **params and returns a dict.
ResponseConfig = dict[str, Any] | Callable[..., dict[str, Any]]

# Type alias for fire-and-forget handler: takes the transport and the
# call's **params, can mutate the transport's state.
FAFHandler = Callable[..., None]


class FakeTransport:
    """Test double for WsTransport.

    Satisfies the same implicit protocol (call / fire_and_forget /
    wait_for_state / wait_for_broadcast) as WsTransport. No network I/O.
    """

    def __init__(self) -> None:
        # Configured responses for `call()`: event -> dict | callable.
        self._responses: dict[str, ResponseConfig] = {}
        # Configured handlers for `fire_and_forget()` — event -> callable.
        self._faf_handlers: dict[str, FAFHandler] = {}
        # Recorded call() invocations: list of (event, params) tuples.
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Recorded fire_and_forget() invocations: list of (event, params) tuples.
        self.fire_and_forget_calls: list[tuple[str, dict[str, Any]]] = []
        # Current cpu.status response. Default: running (stepping=False).
        self._current_state: dict[str, Any] = {"stepping": False}
        # Ticketless broadcast queue (mirrors WsTransport._events_queue).
        self._events_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # Byte-level write overlay (addr -> byte value), consulted by
        # read events so write→read round-trips are consistent. Without
        # it, fixture-backed reads return stale data after a "successful"
        # write, which sends agents into write-read-retry loops
        # (blind-eval finding W2, 2026-09-13).
        self._writes: dict[int, int] = {}

    # ---------- WsTransport-parity introspection ----------

    def is_connected(self) -> bool:
        """Mirror WsTransport.is_connected (F-8 session-health contract).

        The fake is always "connected" — it models a healthy PPSSPP link.
        """
        return True

    # ---------- Configuration ----------

    def set_response(
        self,
        event: str,
        response: ResponseConfig,
    ) -> None:
        """Configure the response returned by `call(event, ...)`.

        Args:
            event: WS event name (e.g. "memory.read_u32").
            response: either a fixed dict, or a callable that accepts
                the call's **params and returns a dict.
        """
        self._responses[event] = response

    def set_faf_handler(
        self,
        event: str,
        handler: FAFHandler,
    ) -> None:
        """Configure a handler invoked when `fire_and_forget(event, ...)` is called.

        The handler is invoked as `handler(self, **params)` and may mutate
        the transport's state (e.g. via `set_state`) to simulate the
        side effect of the fire-and-forget event.
        """
        self._faf_handlers[event] = handler

    def set_state(self, state: dict[str, Any]) -> None:
        """Set the current `cpu.status` response dict."""
        self._current_state = dict(state)

    @property
    def state(self) -> dict[str, Any]:
        """Read-only view of the current `cpu.status` response."""
        return dict(self._current_state)

    @property
    def events(self) -> asyncio.Queue[dict[str, Any]]:
        """Async queue of ticketless broadcast messages (mirrors WsTransport)."""
        return self._events_queue

    def push_broadcast(self, msg: dict[str, Any]) -> None:
        """Enqueue a ticketless broadcast message on the `events` queue.

        Mirrors `WsTransport._recv_loop` pushing ticketless messages to
        `_events_queue`. Used by tests to simulate PPSSPP broadcasts
        (e.g. `cpu.stepping` step-completion notifications).
        """
        self._events_queue.put_nowait(msg)

    # ---------- Implicit WsTransport protocol ----------

    async def call(
        self,
        event: str,
        timeout: float = 5.0,
        **params: Any,
    ) -> dict[str, Any]:
        """Return a configured response for `event`.

        Memory-write events (`memory.write_u8/u16/u32`, `memory.write`)
        are recorded into the byte overlay and return {}. Memory-read
        events (`memory.read_u32`, `memory.read`) consult the overlay
        first — an overlap with written bytes yields a synthesized
        response (unwritten bytes read as 0); otherwise the configured
        response applies. If the response is callable, invokes it with
        the call's **params. If no response is configured, returns an
        empty dict.
        """
        self.calls.append((event, dict(params)))
        if event.startswith("memory.write"):
            self._record_write(event, params)
            if event in self._responses:
                resp = self._responses[event]
                return resp(**params) if callable(resp) else dict(resp)
            return {}
        if event == "memory.read_u32":
            raw = self._overlay_read(int(params.get("address", 0)), 4)
            if raw is not None:
                return {"value": int.from_bytes(raw, "little")}
        elif event == "memory.read":
            size = int(params.get("size", 0) or 0)
            raw = self._overlay_read(int(params.get("address", 0)), size)
            if raw is not None:
                return {"base64": base64.b64encode(raw).decode("ascii")}
        if event == "cpu.status":
            return dict(self._current_state)
        if event in self._responses:
            resp = self._responses[event]
            if callable(resp):
                return resp(**params)
            return dict(resp)
        return {}

    def _record_write(self, event: str, params: dict[str, Any]) -> None:
        """Merge a memory-write event into the byte overlay (little-endian)."""
        addr = int(params.get("address", 0))
        if event == "memory.write_u8":
            data = int(params.get("value", 0)).to_bytes(1, "little")
        elif event == "memory.write_u16":
            data = int(params.get("value", 0)).to_bytes(2, "little")
        elif event == "memory.write_u32":
            data = int(params.get("value", 0)).to_bytes(4, "little")
        else:  # memory.write — base64 payload
            data = base64.b64decode(params.get("base64", ""))
        for i, byte in enumerate(data):
            self._writes[addr + i] = byte

    def _overlay_read(self, addr: int, size: int) -> bytes | None:
        """Synthesize a read over the overlay.

        Returns None when no written byte falls inside [addr, addr+size);
        otherwise returns `size` bytes with written values and 0x00 for
        never-written addresses.
        """
        if size <= 0:
            return None
        hit = any(a in self._writes for a in range(addr, addr + size))
        if not hit:
            return None
        return bytes(self._writes.get(a, 0) for a in range(addr, addr + size))

    async def fire_and_forget(self, event: str, **params: Any) -> None:
        """Record the call and invoke any configured handler.

        The handler is invoked AFTER the call is recorded, so the handler
        can inspect `self.fire_and_forget_calls[-1]` if needed. Handler
        exceptions propagate to the caller (matching WsTransport's
        behavior for `ws.send(...)` failures).
        """
        self.fire_and_forget_calls.append((event, dict(params)))
        if event in self._faf_handlers:
            self._faf_handlers[event](self, **params)

    async def wait_for_state(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout_ms: int = 3000,
        interval_ms: int = 50,
    ) -> dict[str, Any]:
        """Poll the current state until `predicate` is satisfied or timeout.

        Unlike WsTransport.wait_for_state, this does NOT issue a `call()`
        to refresh state — `fire_and_forget()` handlers update state
        synchronously, so the predicate can be checked against
        `_current_state` directly. The polling loop is preserved to
        match WsTransport's semantics for tests that simulate delayed
        state transitions (e.g. via a handler that schedules an
        asyncio task).

        Raises:
            TimeoutError: predicate not satisfied within `timeout_ms`.
        """
        start = time.monotonic()
        timeout_s = timeout_ms / 1000.0
        interval_s = interval_ms / 1000.0
        while True:
            current = dict(self._current_state)
            if predicate(current):
                return current
            elapsed = time.monotonic() - start
            if elapsed >= timeout_s:
                raise TimeoutError(
                    f"wait_for_state timeout ({timeout_ms}ms) — predicate not satisfied"
                )
            await asyncio.sleep(interval_s)

    async def wait_for_broadcast(
        self,
        event: str,
        timeout_ms: int = 5000,
        filter: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Wait for a ticketless broadcast event on the `events` queue.

        Mirrors `WsTransport.wait_for_broadcast` — drains the events
        queue, matches by `event` name + optional `filter`, requeues
        non-matching messages in FIFO order before returning.

        Raises:
            TimeoutError: no matching broadcast within `timeout_ms`.
        """
        start_time = time.monotonic()
        timeout_s = timeout_ms / 1000.0
        backlog: list[dict[str, Any]] = []
        try:
            while True:
                remaining_s = timeout_s - (time.monotonic() - start_time)
                if remaining_s <= 0:
                    raise TimeoutError(
                        f"wait_for_broadcast timeout ({timeout_ms}ms) — "
                        f"no matching '{event}' broadcast"
                    )
                try:
                    msg = await asyncio.wait_for(self._events_queue.get(), timeout=remaining_s)
                except TimeoutError:
                    raise TimeoutError(
                        f"wait_for_broadcast timeout ({timeout_ms}ms) — "
                        f"no matching '{event}' broadcast"
                    ) from None
                if msg.get("event") == event and (filter is None or filter(msg)):
                    return msg
                backlog.append(msg)
        except TimeoutError:
            raise
        finally:
            for msg in backlog:
                self._events_queue.put_nowait(msg)
