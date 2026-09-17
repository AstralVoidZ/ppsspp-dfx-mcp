"""WsTransport — pure WebSocket transport layer for PPSSPP debugger.

Four call semantics: call (ticketed request/response), fire_and_forget,
wait_for_state (poll `cpu.status` until a predicate holds), and
wait_for_broadcast (ticketless event drained from the `events` queue).
The first connection MUST send a version event handshake; responses
are matched by ticket, and ticketless broadcasts are pushed to
`events`.

Thread safety: single-coroutine use. Do not share across coroutines.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import websockets
from websockets.protocol import State

logger = logging.getLogger(__name__)

# ---------- Constants ----------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 12345
WS_PATH = "/debugger"
WS_SUBPROTOCOL = "debugger.ppsspp.org"

# Default polling parameters for wait_for_state.
DEFAULT_WAIT_TIMEOUT_MS = 3000
DEFAULT_WAIT_INTERVAL_MS = 50


class WsTransport:
    """Pure WebSocket transport for PPSSPP debugger.

    Four call semantics:
    - `call()` — send event with ticket, await matching ticket response.
    - `fire_and_forget()` — send event without awaiting response.
    - `wait_for_state()` — poll `cpu.status` until predicate satisfied.
    - `wait_for_broadcast()` — subscribe to a ticketless broadcast event
      from the `events` queue (drain-and-requeue pattern).

    The `events` property is an `asyncio.Queue[dict]` populated by
    `_recv_loop` with ticketless messages (broadcasts).
    """

    def __init__(self, host: str, port: int, verbose: bool = False) -> None:
        self.host = host
        self.port = port
        self.verbose = verbose
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._ticket_counter = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._events_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # PPSSPP version handshake response, kept as build fingerprint
        # evidence (reason/relatedAddress presence differs across dev
        # builds).
        self.version_info: dict[str, Any] | None = None
        self._recv_task: asyncio.Task[None] | None = None
        # Serialize concurrent auto-reconnect attempts.
        self._reconnect_lock = asyncio.Lock()
        # When verbose=True, elevate this module's logger to DEBUG so
        # logger.debug calls actually produce output. The default log
        # level is INFO (config.py DEFAULT_LOG_LEVEL), which silently
        # drops DEBUG messages — making verbose=True a no-op.
        if verbose and logger.getEffectiveLevel() > logging.DEBUG:
            # Elevate only — never lower a level a previous verbose
            # transport set. The module logger is global; per-instance
            # debug emission is still gated by self.verbose.
            logger.setLevel(logging.DEBUG)

    # ---------- Connection lifecycle ----------

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}{WS_PATH}"

    @property
    def events(self) -> asyncio.Queue[dict[str, Any]]:
        """Async queue of ticketless broadcast messages from PPSSPP."""
        return self._events_queue

    async def connect(self) -> None:
        """Connect to PPSSPP WebSocket debugger.

        WebSocket keepalive: explicit ping_interval=20s / ping_timeout=10s.
        PPSSPP's WebsocketServer.cpp auto-replies to ping frames with
        pong, so the WS-layer ping is sufficient for detecting dead
        connections. No application-layer heartbeat is implemented —
        the WS-layer ping covers the need.

        Raises:
            ConnectionRefusedError: PPSSPP not started or port not listening.
            RuntimeError: subprotocol negotiation failed.
        """
        # Defensive: cancel a stale recv task from a previous connection so
        # a reconnect never ends up with two loops draining one socket.
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
            self._recv_task = None
        if self.verbose:
            # Use logger.debug (not print) to keep stdout clean — MCP
            # protocol runs over stdio, any stray stdout output corrupts
            # JSON-RPC frames.
            logger.debug("WS connecting %s (subprotocol: %s)", self.url, WS_SUBPROTOCOL)
        # WS-layer ping is explicitly enabled here; PPSSPP's
        # WebsocketServer auto-replies with PONG. No application-layer
        # heartbeat task.
        self.ws = await websockets.connect(
            self.url,
            subprotocols=[WS_SUBPROTOCOL],
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,  # seconds between WS-layer pings
            ping_timeout=10,  # seconds to wait for pong before disconnecting
            max_size=64 * 1024 * 1024,  # 64MB (screenshots can be large)
        )
        selected = self.ws.subprotocol
        if selected != WS_SUBPROTOCOL:
            raise RuntimeError(
                f"PPSSPP did not select {WS_SUBPROTOCOL} subprotocol (got: {selected}). "
                "Ensure ppsspp.ini has RemoteDebuggerOnStartup = True"
            )
        self._recv_task = asyncio.ensure_future(self._recv_loop())

    def is_connected(self) -> bool:
        """True when the WebSocket is currently open.

        Lets session health report the *actual* connection state
        instead of a flag that is only updated when a tool call
        enters/leaves ``session_client``.
        """
        return self.ws is not None and self.ws.state == State.OPEN

    async def _ensure_connected(self) -> None:
        """Auto-reconnect a dropped session WS.

        The session-level transport is long-lived; without reconnect, a
        single disconnect (e.g. oversized ``memory.readString`` response
        or a missed WS ping) would poison every subsequent call until
        the server restarts — one drop kills the whole session.
        ``call()`` and ``fire_and_forget()`` attempt one reconnect
        before failing.
        """
        if self.is_connected():
            return
        async with self._reconnect_lock:
            # Double-check: another coroutine may have reconnected while
            # we waited for the lock.
            if self.is_connected():
                return
            logger.warning("WS not connected — attempting reconnect to %s", self.url)
            try:
                await self.connect()
                # The protocol header documents the version event as
                # REQUIRED on every new connection, and every
                # first-connect call site pairs connect() with
                # send_version() — the reconnect path must not skip it.
                # Calls still succeed without the handshake on real
                # PPSSPP builds (protocol hygiene, not a functional
                # gate), but the registration state (client
                # name/version) must stay consistent across reconnects.
                # A handshake failure here is a reconnect failure —
                # wrapped with the same RuntimeError as connect.
                await self.send_version()
            except Exception as e:
                raise RuntimeError(
                    f"WebSocket not connected and reconnect to {self.url} failed: {e}"
                ) from e
            logger.info("WS reconnect succeeded (%s)", self.url)

    async def close(self) -> None:
        """Stop the recv loop and close the WebSocket connection.

        All pending call futures are failed with ConnectionError so callers
        awaiting `call()` receive an immediate exception rather than
        hanging until their own timeout fires. This MUST happen before
        `ws.close()` — after the recv loop is cancelled, no further
        responses will be dispatched, so any futures still in `_pending`
        would otherwise leak.
        """
        if self._recv_task:
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
            self._recv_task = None
        # Fail all pending futures before closing the WebSocket — once
        # the recv loop is gone, nothing will ever resolve them.
        pending = list(self._pending.values())
        self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(ConnectionError("WebSocket closed"))
        if self.ws and self.ws.state == State.OPEN:
            await self.ws.close()

    # ---------- Receive loop ----------

    async def _recv_loop(self) -> None:
        """Background receive loop; dispatches ticketed responses to
        `_pending` futures, pushes ticketless messages to `events` queue.
        """
        while self.ws and self.ws.state == State.OPEN:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=0.5)
            except TimeoutError:
                continue
            except websockets.ConnectionClosed:
                # PPSSPP disconnected — fail all pending futures so
                # callers awaiting call() receive an immediate
                # ConnectionError instead of hanging until their own
                # timeout fires. This mirrors the cleanup in close()
                # but without cancelling the recv task (which IS the
                # current coroutine).
                pending = list(self._pending.values())
                self._pending.clear()
                for fut in pending:
                    if not fut.done():
                        fut.set_exception(ConnectionError("PPSSPP WebSocket disconnected"))
                break
            try:
                data = json.loads(raw)
                if self.verbose:
                    logger.debug("WS <<< %s", json.dumps(data, ensure_ascii=False))

                ticket = data.get("ticket")
                if ticket and ticket in self._pending:
                    fut = self._pending.pop(ticket)
                    if data.get("event") == "error":
                        fut.set_exception(
                            RuntimeError(
                                f"PPSSPP error: {data.get('message', 'unknown')} "
                                f"(level={data.get('level')})"
                            )
                        )
                    else:
                        fut.set_result(data)
                else:
                    # Ticketless or unmatched-ticket message → broadcast queue.
                    await self._events_queue.put(data)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A malformed or non-dict message must not kill the recv
                # loop: one bad frame crashing the loop would leave
                # ws.state OPEN, is_connected() True, auto-reconnect
                # disabled, and every subsequent call hanging until its
                # own timeout (a permanently poisoned session). Drop the
                # poison message and keep draining.
                logger.warning(
                    "recv_loop: dropping malformed WS message (%s): %r",
                    e,
                    raw[:200] if isinstance(raw, (str, bytes)) else raw,
                )

    # ---------- Three call semantics ----------

    async def call(self, event: str, timeout: float = 5.0, **params: Any) -> dict[str, Any]:
        """Send event with ticket and await the matching ticket response.

        Raises:
            RuntimeError: WebSocket not connected, or PPSSPP returned an
                error event with matching ticket.
            asyncio.TimeoutError: no matching response within `timeout`.
        """
        if not self.ws or self.ws.state != State.OPEN:
            await self._ensure_connected()

        self._ticket_counter += 1
        ticket = f"t{self._ticket_counter}"
        msg = {"event": event, "ticket": ticket, **params}
        if self.verbose:
            logger.debug("WS >>> %s", json.dumps(msg))

        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[ticket] = fut

        try:
            await self.ws.send(json.dumps(msg))
        except BaseException:
            # A send failure (disconnect race, serialization error) must
            # not strand the future in _pending — if the recv loop
            # survives, nothing would ever resolve it and the entry
            # would leak.
            self._pending.pop(ticket, None)
            raise

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self._pending.pop(ticket, None)
            raise

    async def fire_and_forget(self, event: str, **params: Any) -> None:
        """Send event without awaiting a response.

        NOTE: A ticket IS included in the payload even though we do not
        await a response. The legacy `send_and_forget()` in the original
        monolithic PpssppClient did NOT include a ticket, but PPSSPP may
        rely on tickets for request tracking. Including a ticket but not
        awaiting is the safer default — if PPSSPP echoes a response, the
        recv_loop will simply push it to the `events` queue (since no
        _pending entry is registered for that ticket). The no-ticket
        variant is not used; keeping the ticket is the verified-safe
        choice.
        """
        if not self.ws or self.ws.state != State.OPEN:
            await self._ensure_connected()
        self._ticket_counter += 1
        ticket = f"t{self._ticket_counter}"
        msg = {"event": event, "ticket": ticket, **params}
        if self.verbose:
            logger.debug("WS >>> %s (fire-and-forget)", json.dumps(msg))
        await self.ws.send(json.dumps(msg))

    async def wait_for_state(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout_ms: int = DEFAULT_WAIT_TIMEOUT_MS,
        interval_ms: int = DEFAULT_WAIT_INTERVAL_MS,
    ) -> dict[str, Any]:
        """Poll `cpu.status` until `predicate` returns True or timeout.

        Args:
            predicate: callable that receives the cpu.status response dict
                and returns True when the desired state is reached.
            timeout_ms: total timeout in milliseconds.
            interval_ms: polling interval in milliseconds.

        Returns:
            The first cpu.status response dict satisfying `predicate`.

        Raises:
            TimeoutError: predicate not satisfied within `timeout_ms`.
        """
        start = time.monotonic()
        timeout_s = timeout_ms / 1000.0
        interval_s = interval_ms / 1000.0
        while True:
            status = await self.call("cpu.status")
            if predicate(status):
                return status
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
        """Wait for a ticketless broadcast event from the `events` queue.

        Drains the `events` queue until a message matching `event` (and
        optional `filter`) is found. Non-matching messages are accumulated
        in a local `backlog` and requeued (FIFO order) before the method
        returns, so concurrent consumers (e.g. `send_version` fallback)
        do not lose messages.

        This is the correct primitive for confirming PPSSPP step
        completion — PPSSPP's SteppingBroadcaster pushes `cpu.stepping`
        broadcasts when the CPU enters stepping state
        (SteppingBroadcaster.cpp), which `wait_for_state` polling of
        `cpu.status` cannot observe without race conditions.

        Args:
            event: target broadcast event name (e.g. "cpu.stepping").
            timeout_ms: total timeout in milliseconds. Raises TimeoutError
                if no matching broadcast arrives within this budget.
            filter: optional predicate applied to messages whose `event`
                field already matches. None (default) accepts all such
                messages.

        Returns:
            The matching broadcast message dict.

        Raises:
            RuntimeError: WebSocket not connected (queue cannot grow).
            TimeoutError: no matching broadcast within `timeout_ms`.

        Single-coroutine assumption: do not invoke concurrently with
        other `events` queue consumers (e.g. `send_version` fallback
        path). The drain-and-requeue pattern preserves messages for
        sequential consumers but not concurrent ones.
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
        finally:
            # Requeue non-matching messages in FIFO order on every exit
            # path (success, timeout, cancellation). put_nowait is safe
            # because the queue is unbounded.
            self._requeue(backlog)

    def _requeue(self, backlog: list[dict[str, Any]]) -> None:
        """Requeue a backlog of messages back to the events queue (FIFO).

        Helper for `wait_for_broadcast` — uses `put_nowait` to avoid
        blocking when the queue is non-empty (asyncio.Queue is unbounded
        by default, so put_nowait never blocks on a bounded queue).
        """
        for msg in backlog:
            self._events_queue.put_nowait(msg)

    # ---------- Version handshake ----------

    async def send_version(self) -> dict[str, Any]:
        """Send the version handshake (must be first after connect).

        Strategy: use `call()` with a ticket. If PPSSPP echoes the ticket
        back, the version dict is returned via the ticket path. If PPSSPP
        does NOT echo the ticket (causing `call()` to timeout), fall back
        to polling the `events` queue for an `event == "version"` message.

        Raises:
            RuntimeError: version handshake timeout (5s) on both paths.
        """
        # Try ticket-based call first with a short timeout.
        try:
            resp = await self.call(
                "version",
                timeout=2.0,
                name="ppsspp_dfx_mcp-client",
                version="1.0.0",
            )
            self.version_info = dict(resp)
            return resp
        except (TimeoutError, RuntimeError, ConnectionError):
            # Fallback: poll events queue for a version broadcast.
            pass

        start = time.monotonic()
        while time.monotonic() - start < 5.0:
            try:
                msg = await asyncio.wait_for(self._events_queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            if msg.get("event") == "version":
                self.version_info = dict(msg)
                return msg
            # Put non-version messages back on the queue for other consumers.
            await self._events_queue.put(msg)
        raise RuntimeError("version handshake timeout (5s)")
