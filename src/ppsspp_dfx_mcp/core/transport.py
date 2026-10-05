"""WsTransport — pure WebSocket transport layer for PPSSPP debugger.

Four call semantics: call (ticketed request/response), fire_and_forget,
wait_for_state (poll `cpu.status` until a predicate holds), and
wait_for_broadcast (ticketless event drained from the `events` queue).
The first connection MUST send a version event handshake; responses
are matched by ticket, ticketless broadcasts are pushed to the bounded
drop-oldest `events` queue, and `event == "version"` replies go to their
own queue so the handshake never races another `events` consumer.

Thread safety: single-coroutine use. Do not share across coroutines.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import websockets
from websockets.protocol import State
from websockets.typing import Subprotocol

from ppsspp_dfx_mcp.core.call_diagnostics import CallDiagnostics

logger = logging.getLogger(__name__)

# ---------- Constants ----------

# Total budget for the version handshake.
#
# Measured 2026-10-01: the debugger starts answering ~5s after its port
# opens, so 15s leaves 3x headroom for a cold start. The previous fixed
# split (2s ticket + 5s events) surfaced a misleading
# 'version handshake timeout (5s)' and could not absorb a slow boot.
#
# This is a FAILURE budget too: when a boot wedges both paths
# fail, and every wasted second is multiplied across the tests sharing
# that session -- at 30s a failing session took 350s and cascaded into
# dead attempt to seconds. Per-instance configurable for slow setups.
DEFAULT_HANDSHAKE_TIMEOUT_S = 15.0

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 12345
WS_PATH = "/debugger"
WS_SUBPROTOCOL = "debugger.ppsspp.org"

# Default polling parameters for wait_for_state.
DEFAULT_WAIT_TIMEOUT_MS = 3000
DEFAULT_WAIT_INTERVAL_MS = 50

# Default per-`call` response timeout. Named so budget
# arithmetic can refer to it instead of restating 5.0: wait_for_state
# clamps each poll to what is left of its total budget.
DEFAULT_CALL_TIMEOUT_S = 5.0

# Bound for the dedicated version-handshake queue. The
# handshake reply is a one-shot broadcast, so a handful of slots is plenty;
# drop-oldest keeps the newest reply addressable if a peer ever spams it.
_VERSION_QUEUE_MAX = 8

# Bound for the shared broadcast queue. Ticketless frames
# arrive from PPSSPP pushes and from echoes of our own fire-and-forget
# commands; a session that keeps receiving them faster than anything
# consumes them used to grow the queue without limit (an observer-less
# session, or a stalled consumer). Drop-oldest on overflow matches the
# GameStateObserver per-event queues: losing a stale frame beats unbounded
# memory growth. Sized well above any single wait_for_broadcast window so
# a normal consumer never loses a frame it is waiting for.
_EVENTS_QUEUE_MAX = 256

# How many recent fire-and-forget tickets to remember, so their echoes can
# be told apart from genuine broadcasts. Bounded: an echo
# that never arrives must not accumulate state forever, and an aged-out
# ticket simply stops being recognised as ours.
_FIRE_AND_FORGET_TICKET_MEMORY = 64


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

    def __init__(
        self,
        host: str,
        port: int,
        handshake_timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
    ) -> None:
        self.handshake_timeout_s = handshake_timeout_s
        self.host = host
        self.port = port
        self.ws: websockets.WebSocketClientProtocol | None = None
        self._ticket_counter = 0
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        # Bounded and drop-oldest: see _put_event().
        self._events_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_EVENTS_QUEUE_MAX)
        # Running count of frames dropped by the bound above, for tests and
        # for diagnosing "the broadcast I waited for vanished".
        self._events_dropped = 0
        # Tickets we sent via fire_and_forget(). Their echoes come back
        # ticketless-matched (no `_pending` entry) and must not be mistaken
        # for independent broadcast evidence.
        self._ff_tickets: deque[str] = deque(maxlen=_FIRE_AND_FORGET_TICKET_MEMORY)
        # Version broadcasts get their own queue so the
        # handshake fallback never races another `events` consumer. On a
        # reconnect the GameStateObserver dispatcher is already draining
        # `events`, and `"version"` is not in its `_SUBSCRIBED_EVENTS`, so it
        # consumed and silently dropped the reply the fallback was waiting
        # for: `send_version` exhausted its budget, the reconnect failed, and
        # every later call on that session failed too. A queue whose only
        # consumer is `send_version` makes that race structurally impossible.
        self._version_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=_VERSION_QUEUE_MAX
        )
        # Ticketed-call audit trail. `call()` is the only
        # place that sees both halves of a request/response exchange, so
        # the "why did the response never come" question is answered from
        # here.
        # Bounded ring buffer; never unbounded (see call_diagnostics).
        self.diagnostics = CallDiagnostics()
        # Re-entrancy latch for _probe_stepping_best_effort: a probe issues
        # a call(), and a call() that times out probes again.
        self._probing_stepping = False
        # PPSSPP version handshake response, kept as build fingerprint
        # evidence (reason/relatedAddress presence differs across dev
        # builds).
        self.version_info: dict[str, Any] | None = None
        self._recv_task: asyncio.Task[None] | None = None
        # Serialize concurrent auto-reconnect attempts.
        self._reconnect_lock = asyncio.Lock()

    # ---------- Connection lifecycle ----------

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}{WS_PATH}"

    @property
    def events(self) -> asyncio.Queue[dict[str, Any]]:
        """Async queue of ticketless broadcast messages from PPSSPP.

        Bounded (``_EVENTS_QUEUE_MAX``) and drop-oldest on overflow, so a
        consumer that stops draining cannot grow it without limit.
        """
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
        # cancel() alone leaves a scheduling window in
        # which the old loop can still see the (about-to-be-replaced)
        # shared self.ws — awaiting the task makes the handover
        # structural instead of timing luck.
        if self._recv_task is not None and not self._recv_task.done():
            self._recv_task.cancel()
            # The task's own CancelledError (from the cancel() above) or
            # connection errors are the expected shutdown modes; a cancel
            # of THIS connect() call still propagates via the awaiting
            # task's machinery elsewhere.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._recv_task
            self._recv_task = None
        # An explicit reconnect over an OPEN socket used
        # to overwrite self.ws without closing it — socket fd leak.
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
            self.ws = None
        # WS-layer ping is explicitly enabled here; PPSSPP's
        # WebsocketServer auto-replies with PONG. No application-layer
        # heartbeat task.
        self.ws = await websockets.connect(
            self.url,
            subprotocols=[Subprotocol(WS_SUBPROTOCOL)],
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,  # seconds between WS-layer pings
            ping_timeout=10,  # seconds to wait for pong before disconnecting
            max_size=64 * 1024 * 1024,  # 64MB (screenshots can be large)
        )
        selected = self.ws.subprotocol
        if selected != WS_SUBPROTOCOL:
            # The socket must not stay assigned: _recv_task never started,
            # so an OPEN socket here means is_connected() reports True and
            # _ensure_connected() short-circuits forever — every later call
            # burns its full timeout against a socket nobody drains.
            with contextlib.suppress(Exception):
                await self.ws.close()
            self.ws = None
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
        `_pending` futures, pushes ticketless messages to `events`, and
        routes ticketless version replies to `_version_queue`.
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
                    elif fut.cancelled():
                        # The waiting caller was cancelled — its future
                        # is dead. Drop the response instead of raising
                        # InvalidStateError from set_result (which the recv
                        # loop mislogs as a malformed message).
                        pass
                    else:
                        fut.set_result(data)
                else:
                    # Ticketless or unmatched-ticket message → broadcast queue.
                    # T007: a broadcast for an event with a call in flight is
                    # evidence the response path is alive for that event, which
                    # is what separates "no response at all" from "responses
                    # flowing but not matching this ticket".
                    event_name = str(data.get("event", ""))
                    # An echo of a command WE sent via
                    # fire_and_forget() proves the peer answered us, not that
                    # this event's producer is alive, so it must not count as
                    # timeout evidence. `_ff_tickets` is bounded; a ticket
                    # that aged out simply stops being recognised as ours.
                    if not (ticket and ticket in self._ff_tickets):
                        self.diagnostics.note_broadcast(event_name)
                    if event_name == "version":
                        # The handshake reply goes to its own
                        # queue (see __init__) instead of `events`, which a live
                        # dispatcher would drain and drop.
                        if self._version_queue.full():
                            with contextlib.suppress(asyncio.QueueEmpty):
                                self._version_queue.get_nowait()
                        self._version_queue.put_nowait(data)
                    else:
                        self._put_event(data)
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

    async def call(
        self, event: str, timeout: float = DEFAULT_CALL_TIMEOUT_S, **params: Any
    ) -> dict[str, Any]:
        """Send event with ticket and await the matching ticket response.

        Raises:
            RuntimeError: WebSocket not connected, or PPSSPP returned an
                error event with matching ticket.
            asyncio.TimeoutError: no matching response within `timeout`.
        """
        if not self.ws or self.ws.state != State.OPEN:
            await self._ensure_connected()
        # _ensure_connected() 要么留下已连接的 socket，要么抛错——此处守卫仅为
        # 向类型检查器传达该后置条件。
        ws = self.ws
        if ws is None:
            raise RuntimeError(f"WebSocket not connected: {self.url}")

        self._ticket_counter += 1
        ticket = f"t{self._ticket_counter}"
        msg = {"event": event, "ticket": ticket, **params}

        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[ticket] = fut
        record = self.diagnostics.begin(event, ticket)

        try:
            await ws.send(json.dumps(msg))
        except BaseException as exc:
            # A send failure (disconnect race, serialization error) must
            # not strand the future in _pending — if the recv loop
            # survives, nothing would ever resolve it and the entry
            # would leak.
            self._pending.pop(ticket, None)
            self.diagnostics.settle(record, error=f"{type(exc).__name__}: {exc}")
            raise

        try:
            response = await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError:
            self._pending.pop(ticket, None)
            # `stepping` is the discriminator for the timeout contract: a paused
            # CPU renders no frames, so a frame-driven event can never be
            # answered. Probed best-effort with a short budget — diagnostics
            # must never be the reason a call fails, nor add a second full
            # timeout to a call that already burned its budget.
            stepping = await self._probe_stepping_best_effort()
            self.diagnostics.settle(
                record,
                timed_out=True,
                stepping=stepping,
                error=f"no response within {timeout}s",
            )
            raise
        except BaseException as exc:
            # Cancellation (client disconnect / task cancel) also
            # strands the ticket — pop it, or the late response would hit
            # the recv loop's set_result on a cancelled future and be
            # mislogged as a malformed message.
            self._pending.pop(ticket, None)
            self.diagnostics.settle(record, error=f"{type(exc).__name__}: {exc}")
            raise

        self.diagnostics.settle(record)
        return response

    async def _probe_stepping_best_effort(self) -> bool | None:
        """Ask PPSSPP whether the CPU is stepping; None if unanswerable.

        Uses the documented `cpu.status` request rather than a cached flag
        because the transport keeps no running state cache: the only
        sources are the poll in `wait_for_state` and the `cpu.stepping`
        broadcast, neither of which is guaranteed to have fired. Returns
        None (not False) when the probe itself fails, so "unknown" is never
        confused with "running".

        Re-entrancy guard is mandatory, not defensive: this issues a
        `call()`, and a `call()` that times out probes stepping again, so
        without the guard an unresponsive debugger recurses until the
        event loop dies. The probe therefore gives up immediately when it
        is already running.
        """
        if self._probing_stepping:
            return None
        self._probing_stepping = True
        try:
            resp = await self.call("cpu.status", timeout=0.5)
        except Exception as exc:  # noqa: BLE001 — diagnostics are best-effort
            logger.debug("diagnostics: cpu.status probe failed: %s", exc)
            return None
        finally:
            self._probing_stepping = False
        if not isinstance(resp, dict):
            return None
        value = resp.get("stepping")
        return None if value is None else bool(value)

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
        # 同上：把 _ensure_connected() 的后置条件收窄为局部变量。
        ws = self.ws
        if ws is None:
            raise RuntimeError(f"WebSocket not connected: {self.url}")
        self._ticket_counter += 1
        ticket = f"t{self._ticket_counter}"
        # Remember it (bounded) so the echo can be told apart from a genuine
        # broadcast in _recv_loop.
        self._ff_tickets.append(ticket)
        msg = {"event": event, "ticket": ticket, **params}
        await ws.send(json.dumps(msg))

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
            # Each poll `call` must fit inside what is left
            # of the declared budget. With the old shape the default 5s call
            # timeout was invisible to the caller's budget, so a declared
            # 3000ms wait could actually take 5s+ per poll.
            #
            # The poll is CLAMPED, never skipped: a poll that succeeds still
            # gets to satisfy the predicate first (the `timeout_ms=1` case in
            # test_transport_orchestration.TestWaitForState documents that
            # tolerance), and the deadline below stays authoritative.
            remaining_s = timeout_s - (time.monotonic() - start)
            try:
                status = await self.call(
                    "cpu.status", timeout=max(0.1, min(DEFAULT_CALL_TIMEOUT_S, remaining_s))
                )
            except TimeoutError as e:
                # Surface the caller's own semantic message (and budget),
                # not the per-poll call timeout.
                raise TimeoutError(
                    f"wait_for_state timeout ({timeout_ms}ms) — predicate not satisfied "
                    f"(last poll: {e})"
                ) from e
            if predicate(status):
                return status
            if time.monotonic() - start >= timeout_s:
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
        returns, so sequential consumers do not lose messages. The version
        handshake no longer shares this queue — it has its own
        `_version_queue` — so the only remaining concurrent
        consumer is the GameStateObserver dispatcher.

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
        other `events` queue consumers — the GameStateObserver dispatcher is
        one. The drain-and-requeue pattern preserves messages for
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
            # path (success, timeout, cancellation). Drop-oldest if the
            # queue refilled while we were draining (`_put_event`).
            self._requeue(backlog)

    def _put_event(self, data: dict[str, Any]) -> None:
        """Enqueue a broadcast, dropping the oldest frame when at capacity.

        `_events_queue` used to be unbounded, and every
        ticketless frame — including the echo of each fire-and-forget
        command — was appended and never discarded, so a long session grew
        it without limit. Bounded + drop-oldest mirrors the
        GameStateObserver per-event queues: losing a stale frame beats
        unbounded memory growth, and the newest frames stay addressable.
        """
        if self._events_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._events_queue.get_nowait()
                self._events_dropped += 1
                logger.debug(
                    "events queue full (%d); dropped oldest broadcast (total dropped: %d)",
                    _EVENTS_QUEUE_MAX,
                    self._events_dropped,
                )
        self._events_queue.put_nowait(data)

    def _requeue(self, backlog: list[dict[str, Any]]) -> None:
        """Requeue a backlog of messages back to the events queue (FIFO).

        Helper for `wait_for_broadcast`. Uses `_put_event`, so a queue that
        refilled while the caller was draining still honours the bounded
        drop-oldest policy instead of raising QueueFull.
        """
        for msg in backlog:
            self._put_event(msg)

    # ---------- Version handshake ----------

    async def send_version(self) -> dict[str, Any]:
        """Send the version handshake (must be first after connect).

        Strategy: use `call()` with a ticket. If PPSSPP echoes the ticket
        back, the version dict is returned via the ticket path. If PPSSPP
        does NOT echo the ticket (causing `call()` to timeout), fall back
        to `_version_queue`, where `_recv_loop` routes every ticketless
        `event == "version"` message. That queue has no
        other consumer, so the reply cannot be stolen and dropped by the
        GameStateObserver dispatcher during a reconnect.

        Raises:
            RuntimeError: version handshake timeout on both paths. The
                budget is ``handshake_timeout_s`` (default 15s).
        """
        # Ticket path gets a short slice; the remainder polls the version queue.
        budget = max(self.handshake_timeout_s, 1.0)
        # Drop any reply left over from an earlier handshake so it cannot be
        # mistaken for this one's. Done BEFORE the ticket attempt: a reply that
        # arrives while `call()` is waiting (ticket not echoed) is this
        # handshake's and must survive into the fallback below.
        with contextlib.suppress(asyncio.QueueEmpty):
            while True:
                self._version_queue.get_nowait()
        try:
            resp = await self.call(
                "version",
                timeout=min(2.0, budget / 2),
                name="ppsspp_dfx_mcp-client",
                version="1.0.0",
            )
            self.version_info = dict(resp)
            return resp
        except (TimeoutError, RuntimeError, ConnectionError):
            # Fallback: poll events queue for a version broadcast.
            pass

        start = time.monotonic()
        while time.monotonic() - start < budget - min(2.0, budget / 2):
            try:
                msg = await asyncio.wait_for(self._version_queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            if msg.get("event") == "version":
                self.version_info = dict(msg)
                return msg
            # Unreachable in practice: `_recv_loop` routes only version
            # messages here. Drop instead of requeueing so a bounded queue
            # can never wedge on a protocol surprise.
            logger.warning("send_version: ignoring unexpected non-version message %r", msg)
        raise RuntimeError(f"version handshake timeout ({budget:.0f}s)")
