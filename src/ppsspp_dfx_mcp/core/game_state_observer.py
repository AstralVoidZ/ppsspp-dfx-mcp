"""GameStateObserver — game lifecycle broadcast subscriber.

Subscribes to PPSSPP's broadcast event types via a single-consumer
dispatcher, maintains a game state machine (loading → running → paused
→ running / running → quit), and drives freeze detection + diagnostic
log injection.

Lifecycle: owned by SessionManager — created on session start, stopped
on session stop. Does NOT own the transport lifecycle; the caller is
responsible for connect/close.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

from ppsspp_dfx_mcp.core.transport import WsTransport
from ppsspp_dfx_mcp.logging import PPSSPP_LOG_LOGGER_NAME

logger = logging.getLogger(__name__)

# PPSSPP log broadcast level → Python logging level mapping.
# See PPSSPP LogBroadcaster.cpp — level is numeric 1-6:
#   1=NOTICE, 2=ERROR, 3=WARN, 4=INFO, 5=DEBUG, 6=VERBOSE.
_LOG_LEVEL_MAP: dict[int, int] = {
    1: logging.INFO,  # NOTICE
    2: logging.ERROR,  # ERROR
    3: logging.WARNING,  # WARN
    4: logging.INFO,  # INFO
    5: logging.DEBUG,  # DEBUG
    6: logging.DEBUG,  # VERBOSE
}

# Subscribed event names — each gets its own asyncio.Queue.
# "cpu.stepping" is subscribed here so step confirmation
# (debug_client._step_with_retry / _confirm_step_completed) consumes
# from THIS dedicated queue instead of racing the dispatcher on
# transport.events: without it, the dispatcher deterministically
# consumes (and silently drops) every cpu.stepping broadcast, leaving
# transport.wait_for_broadcast('cpu.stepping') dead on production
# session-level transports.
_SUBSCRIBED_EVENTS: tuple[str, ...] = (
    "game.start",
    "game.quit",
    "game.pause",
    "game.resume",
    "log",
    "cpu.resume",
    "cpu.stepping",
    "gpu.stats.get",
)

# Game lifecycle event names (state machine inputs).
_GAME_EVENTS: frozenset[str] = frozenset({"game.start", "game.quit", "game.pause", "game.resume"})

# Valid state machine transitions: {from_state: {event_name: to_state}}.
# The quit→loading transition is NOT here because it is triggered by
# session reset (SessionManager), not by a broadcast.
_TRANSITIONS: dict[str, dict[str, str]] = {
    "loading": {"game.start": "running"},
    "running": {
        "game.pause": "paused",
        "game.quit": "quit",
    },
    "paused": {"game.resume": "running"},
    "quit": {},  # terminal until session reset
}


class GameStateObserver:
    """Game lifecycle broadcast subscriber + freeze detector.

    Single-consumer dispatcher pattern: one background coroutine
    consumes ``transport.events`` queue and dispatches messages by
    ``event`` field to per-event-name ``asyncio.Queue`` instances.
    Subscribers consume from their dedicated queues, avoiding the
    drain-and-requeue conflict of concurrent ``wait_for_broadcast``
    calls.

    State machine:
        loading → running  (game.start)
        running → paused   (game.pause)
        paused  → running  (game.resume)
        running → quit      (game.quit)
        quit    → loading   (session reset, via SessionManager)

    Invalid transitions are logged as warnings but do not raise —
    tolerates missed broadcasts (e.g. late subscriber startup).
    """

    def __init__(self, transport: WsTransport) -> None:
        self._transport = transport
        # State machine — initial state is "loading".
        self._state: str = "loading"
        # Per-event-name queues for the single-consumer dispatcher.
        # The game.* queues are INTENTIONALLY consumer-less today — the
        # state machine transition is applied inline by the dispatcher
        # (_apply_transition). The queues exist to preserve the
        # per-event-name subscription contract for future subscribers;
        # each holds at most one message per lifecycle event, so the
        # footprint is bounded and negligible.
        self._queues: dict[str, asyncio.Queue[dict[str, Any]]] = {
            name: asyncio.Queue() for name in _SUBSCRIBED_EVENTS
        }
        # Fan-out subscribers for "cpu.stepping". The dedicated queue
        # above REMAINS the step-confirmation buffer (its single
        # consumer is debug_client._confirm_step_completed — it must
        # keep buffering broadcasts that arrive between the step command
        # and the confirmation wait). Subscribers get their OWN bounded
        # queues fed by the dispatcher, so a breakpoint-wait tool never
        # steals step confirmations and vice versa (multi-consumer
        # fan-out; see subscribe_stepping / SteppingSubscription).
        self._stepping_subscribers: list[SteppingSubscription] = []
        # Background tasks — tracked for cancellation on stop().
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._log_consumer_task: asyncio.Task[None] | None = None
        self._gpu_stats_consumer_task: asyncio.Task[None] | None = None
        self._gpu_freeze_task: asyncio.Task[None] | None = None
        # gpu.stats.feed tracking state. Monotonic clock, not
        # wall-clock datetime — system time jumps produced negative/huge
        # elapsed values in the freeze detector.
        self._last_frame_mono: float | None = None
        self._gpu_stats_feed_enabled: bool = False
        # GPU freeze detection flag — set True when _gpu_freeze_detector
        # detects a frame freeze. Queried by callers via
        # was_gpu_freeze_detected(). Reset on start_gpu_stats_feed().
        # Set instead of raised because the detector runs as a
        # background task nobody awaits at runtime — a raised exception
        # would be silently lost.
        self._gpu_freeze_detected: bool = False

    # ------------------------------------------------------------------
    # Public API — state machine
    # ------------------------------------------------------------------

    def get_state(self) -> str:
        """Return current state machine state.

        Returns one of ``"loading"`` / ``"running"`` / ``"paused"`` /
        ``"quit"``. Used by freeze detection to suppress false alarms
        (e.g. player paused → no frames expected).
        """
        return self._state

    def was_gpu_freeze_detected(self) -> bool:
        """Return True if GPU freeze was detected since last feed start.

        Set by ``_gpu_freeze_detector`` when no frame arrives for >3s
        while game state is ``running``. Reset on
        ``start_gpu_stats_feed()``. Callers (e.g. ``_require_running``
        or ``to_tool_error`` translation paths) can query this as an
        additional freeze signal.

        The detector sets this flag instead of raising because it runs
        in a background task that no one awaits at runtime — a raised
        exception would be silently lost. The flag makes the detection
        observable.
        """
        return self._gpu_freeze_detected

    # ------------------------------------------------------------------
    # Public API — lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the dispatcher + defensively confirm log broadcast.

        Idempotent: safe to call multiple times (subsequent calls are
        no-ops). Starts:
        - Single-consumer dispatcher coroutine.
        - Log broadcast consumer coroutine.
        - Defensive ``broadcast.config.set`` call (log is enabled by
          default in PPSSPP; this call explicitly confirms).

        Does NOT start gpu.stats.feed tracking — call
        ``start_gpu_stats_feed()`` separately to enable frame freeze
        detection.

        Note on ``broadcast.config.set`` field name: the disallowed
        field is ``logger`` (not ``log``); ``disallowed.logger=False``
        means "not disabled" i.e. enabled. Log broadcast is enabled by
        default in PPSSPP; this call is defensive.
        """
        if self._dispatcher_task is not None:
            return  # already started

        # Defensive: confirm log broadcast is enabled. Field name is
        # `logger` (not `log`); disallowed.logger=False means "not
        # disabled" i.e. enabled. Log broadcast is enabled by default
        # in PPSSPP; this call is defensive.
        try:
            await self._transport.call("broadcast.config.set", disallowed={"logger": False})
        except Exception as e:
            logger.warning("start: broadcast.config.set failed (defensive, non-fatal): %s", e)

        # Start the single-consumer dispatcher.
        self._dispatcher_task = asyncio.ensure_future(self._dispatcher())
        # Start the log consumer (subscribes to the log queue).
        self._log_consumer_task = asyncio.ensure_future(self._log_consumer())
        # gpu.stats.feed tracker is started on demand via
        # start_gpu_stats_feed().

    async def stop(self) -> None:
        """Cancel all background coroutines.

        Cancels dispatcher / log consumer / gpu.stats consumer / gpu
        freeze detector. Does NOT close the transport — that is the
        caller's responsibility (SessionManager).

        Cancellation order: cancel-all → await-all → setattr-None, so
        no other code can observe a None task reference while the task
        is still running.
        """
        # Collect tasks first (without clearing attrs yet).
        attrs = (
            "_dispatcher_task",
            "_log_consumer_task",
            "_gpu_stats_consumer_task",
            "_gpu_freeze_task",
        )
        tasks_to_cancel: list[asyncio.Task[None]] = []
        for attr in attrs:
            task = getattr(self, attr)
            if task is not None:
                tasks_to_cancel.append(task)
        # Cancel all tasks first (requests cancellation; does not wait).
        for task in tasks_to_cancel:
            task.cancel()
        # Await all tasks to ensure they have actually terminated.
        for task in tasks_to_cancel:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning("stop: task cleanup error: %s", e)
        # Finally clear the attrs — safe now that tasks are done.
        for attr in attrs:
            setattr(self, attr, None)
        self._gpu_stats_feed_enabled = False

    # ------------------------------------------------------------------
    # Public API — cpu.resume broadcast
    # ------------------------------------------------------------------

    async def wait_for_resume(self, timeout_ms: int = 3000) -> bool:
        """Wait for a ``cpu.resume`` broadcast.

        Consumes from the ``cpu.resume`` per-event-name queue (NOT
        directly from ``transport.events``). PPSSPP's SteppingBroadcaster
        (SteppingBroadcaster.cpp:64) pushes ``cpu.resume`` when
        ``prevState_ == CORE_STEPPING && coreState != CORE_STEPPING
        && Core_IsActive()``.

        Single-consumer constraint: only one
        coroutine may await this method at a time. Concurrent calls
        will race on the single ``cpu.resume`` queue — the first
        awaiter consumes the broadcast, the second blocks until
        timeout. ``SteppingManager.resume()`` is the sole caller and
        is never invoked concurrently (single-coroutine use contract
        of ``PpssppDebugClient``).

        Args:
            timeout_ms: total timeout in milliseconds (default 3000).

        Returns:
            True if a ``cpu.resume`` broadcast arrives within
            ``timeout_ms``; False on timeout.
        """
        queue = self._queues["cpu.resume"]
        try:
            await asyncio.wait_for(queue.get(), timeout=timeout_ms / 1000.0)
            return True
        except TimeoutError:
            return False

    def is_running(self) -> bool:
        """True when the dispatcher coroutine is started and still alive.

        Consumers (e.g. ``PpssppDebugClient._wait_step_broadcast``) use
        this to decide whether per-event-name queues are being fed — if
        the dispatcher is not running, nothing will ever be enqueued and
        consuming from the dedicated queues would hang until timeout.
        """
        task = self._dispatcher_task
        return task is not None and not task.done()

    def drain_resume(self) -> None:
        """Drop STALE ``cpu.resume`` broadcasts.

        ``wait_for_resume`` accepts ANY queued ``cpu.resume`` broadcast —
        including one left over from a PREVIOUS resume cycle (a broadcast
        that arrived after that cycle's 3s wait window and poll fallback
        had already returned stays in the queue indefinitely). Without
        draining, the next ``SteppingManager.resume()`` can be "confirmed"
        in 0ms by the leftover broadcast before PPSSPP has processed the
        new resume command.

        Called by ``SteppingManager.resume()`` right before it issues the
        new ``cpu.resume`` command, so only a broadcast produced by THIS
        resume can satisfy the wait. Real-PPSSPP probes: the happy path
        leaves the queue empty (one broadcast per resume, consumed by the
        wait), so draining is a no-op there — the cost is zero.
        """
        queue = self._queues["cpu.resume"]
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def wait_for_step_broadcast(
        self,
        timeout_ms: int = 1200,
        filter: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any] | None:
        """Wait for a ``cpu.stepping`` broadcast.

        Consumes from the ``cpu.stepping`` per-event-name queue fed by
        the single-consumer dispatcher. This is the step-confirmation
        path for session-level transports:
        ``transport.wait_for_broadcast('cpu.stepping')`` cannot be used
        there — it races the dispatcher on ``transport.events`` and
        deterministically loses (the dispatcher is parked at the head
        of the getter queue), so every step broadcast is consumed and
        silently dropped.

        Callers must check ``is_running()`` first (or fall back to
        ``transport.wait_for_broadcast``) — with the dispatcher stopped,
        the dedicated queue is never fed.

        Args:
            timeout_ms: total wait budget in milliseconds.
            filter: optional predicate applied to each broadcast; a
                broadcast rejected by the filter is consumed and
                dropped (matching ``wait_for_broadcast`` semantics).

        Returns:
            The first matching broadcast dict, or None on timeout.
        """
        queue = self._queues["cpu.stepping"]
        deadline = time.monotonic() + timeout_ms / 1000.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=remaining)
            except TimeoutError:
                return None
            if filter is None or filter(msg):
                return msg
            # Filter-rejected broadcast is consumed and dropped (same
            # semantics as WsTransport.wait_for_broadcast).

    # ------------------------------------------------------------------
    # Public API — cpu.stepping fan-out subscriptions
    # ------------------------------------------------------------------

    def subscribe_stepping(self) -> SteppingSubscription:
        """Register a fan-out subscriber for ``cpu.stepping`` broadcasts.

        Unlike ``wait_for_step_broadcast`` (which consumes the shared
        step-confirmation queue and must not run concurrently with a
        step), each subscription has its OWN bounded queue fed by the
        dispatcher from subscription time — broadcasts buffer even while
        nobody awaits, and multiple subscribers never steal from each
        other. The subscriber MUST be released via ``close()`` (or the
        async-context-manager form) so the dispatcher stops feeding it.

        Returns:
            A fresh ``SteppingSubscription``.
        """
        sub = SteppingSubscription(self)
        self._stepping_subscribers.append(sub)
        return sub

    def _remove_stepping_subscription(self, sub: SteppingSubscription) -> None:
        """Detach a subscriber (idempotent; called by ``sub.close()``)."""
        with contextlib.suppress(ValueError):
            self._stepping_subscribers.remove(sub)

    # ------------------------------------------------------------------
    # Public API — gpu.stats.feed
    # ------------------------------------------------------------------

    async def start_gpu_stats_feed(self) -> None:
        """Enable ``gpu.stats.feed`` and start frame freeze detection.

        Calls ``transport.call("gpu.stats.feed", enable=True)`` (default
        is also True, explicit is clearer) and starts two background
        coroutines:
        - gpu.stats.get consumer: updates ``last_frame_at`` on each
          broadcast.
        - gpu freeze detector: raises ``CpuFreezeSuspected`` if no
          frame for >3s while game state is ``running``.

        Idempotent: safe to call multiple times (subsequent calls are
        no-ops).
        """
        if self._gpu_stats_feed_enabled:
            return
        await self._transport.call("gpu.stats.feed", enable=True)
        self._gpu_stats_feed_enabled = True
        self._last_frame_mono = time.monotonic()
        # Reset freeze flag for this feed session.
        self._gpu_freeze_detected = False
        if self._gpu_stats_consumer_task is None:
            self._gpu_stats_consumer_task = asyncio.ensure_future(self._gpu_stats_consumer())
        if self._gpu_freeze_task is None:
            self._gpu_freeze_task = asyncio.ensure_future(self._gpu_freeze_detector())

    async def stop_gpu_stats_feed(self) -> None:
        """Disable ``gpu.stats.feed`` and stop frame freeze detection.

        Calls ``transport.call("gpu.stats.feed", enable=False)`` and
        cancels the gpu.stats.get consumer + gpu freeze detector
        coroutines.

        Idempotent: safe to call multiple times (subsequent calls are
        no-ops).
        """
        if not self._gpu_stats_feed_enabled:
            return
        try:
            await self._transport.call("gpu.stats.feed", enable=False)
        except Exception as e:
            logger.warning("stop_gpu_stats_feed: gpu.stats.feed disable failed: %s", e)
        self._gpu_stats_feed_enabled = False
        for attr in ("_gpu_stats_consumer_task", "_gpu_freeze_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.warning("stop_gpu_stats_feed: cleanup error: %s", e)
                setattr(self, attr, None)

    # ------------------------------------------------------------------
    # Single-consumer dispatcher
    # ------------------------------------------------------------------

    async def _dispatcher(self) -> None:
        """Single-consumer dispatcher.

        Consumes from ``transport.events`` queue once, dispatches to
        per-event-name ``asyncio.Queue``. Single message processing
        failures are logged as warnings but do not crash the loop.

        For ``game.*`` events, the state machine transition is applied
        synchronously AFTER dispatching to the per-event-name queue.
        This keeps state machine updates low-latency (no extra
        consumer coroutine hop) while still satisfying the
        per-event-name queue contract for future subscribers.
        """
        while True:
            try:
                msg = await self._transport.events.get()
                event_name = msg.get("event")
                # Dispatch to per-event-name queue (if subscribed).
                if event_name in self._queues:
                    await self._queues[event_name].put(msg)
                # Fan-out to cpu.stepping subscribers: each has its own
                # bounded queue (drop-oldest on overflow), so one slow
                # subscriber never starves the step-confirmation queue
                # or other subscribers.
                if event_name == "cpu.stepping":
                    # Mirror the RAW broadcast JSON so
                    # ppsspp_analyze_log(filter="cpu.stepping")
                    # can retrieve exact wire evidence (reason /
                    # relatedAddress presence differs across PPSSPP dev
                    # builds). INFO: the mirror logger inherits the root
                    # INFO gate; one line per step/hit is cheap.
                    logging.getLogger(PPSSPP_LOG_LOGGER_NAME).info(
                        "[ws.raw] %s", json.dumps(msg, default=str)
                    )
                    for sub in list(self._stepping_subscribers):
                        sub._offer(msg)
                # Apply game state machine transition for game.* events.
                if event_name in _GAME_EVENTS:
                    self._apply_transition(event_name)
                # Unknown event names are silently dropped (defensive).
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Log full traceback for non-expected exceptions (e.g.
                # state machine logic bugs) so they are not silently
                # swallowed.
                logger.warning(
                    "dispatcher: message handling failed: %s",
                    e,
                    exc_info=True,
                )

    def _apply_transition(self, event_name: str) -> None:
        """Apply a game.* state machine transition.

        Valid transitions update ``self._state``. Invalid transitions
        are logged as warnings but do not raise (tolerate missed
        broadcasts).
        """
        transitions = _TRANSITIONS.get(self._state, {})
        new_state = transitions.get(event_name)
        if new_state is not None:
            logger.debug("state transition: %s --%s--> %s", self._state, event_name, new_state)
            self._state = new_state
        else:
            logger.warning(
                "invalid state transition: event=%s, current=%s", event_name, self._state
            )

    # ------------------------------------------------------------------
    # Log broadcast consumer
    # ------------------------------------------------------------------

    async def _log_consumer(self) -> None:
        """Consume ``log`` broadcasts and inject to Python logging.

        Malformed broadcasts (missing ``level`` or ``message``) are
        silently dropped (not crash the observer).
        """
        queue = self._queues["log"]
        ppsspp_logger = logging.getLogger("ppsspp_dfx_mcp.ppsspp_log")
        while True:
            try:
                msg = await queue.get()
                self._inject_log(msg, ppsspp_logger)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("log_consumer: error: %s", e)

    @staticmethod
    def _inject_log(msg: dict[str, Any], ppsspp_logger: logging.Logger) -> None:
        """Inject a single log broadcast into the Python logger.

        Malformed broadcasts (missing ``level`` or ``message``) are
        silently dropped.

        Log broadcast fields: ``event`` / ``timestamp`` / ``header`` /
        ``message`` / ``level`` (numeric 1-6) / ``channel``.

        Message format: ``"[<channel>] <header>: <message>"``.
        """
        level_num = msg.get("level")
        message = msg.get("message")
        if level_num is None or message is None:
            return  # silently drop malformed broadcasts
        try:
            level_int = int(level_num)
        except TypeError, ValueError:
            return  # invalid level, drop
        py_level = _LOG_LEVEL_MAP.get(level_int)
        if py_level is None:
            return  # unknown level, drop
        channel = msg.get("channel", "")
        header = msg.get("header", "")
        formatted = f"[{channel}] {header}: {message}"
        ppsspp_logger.log(py_level, formatted)

    # ------------------------------------------------------------------
    # gpu.stats.get consumer
    # ------------------------------------------------------------------

    async def _gpu_stats_consumer(self) -> None:
        """Consume ``gpu.stats.get`` broadcasts and update last_frame_at."""
        queue = self._queues["gpu.stats.get"]
        while True:
            try:
                await queue.get()
                self._last_frame_mono = time.monotonic()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("gpu_stats_consumer: error: %s", e)

    # ------------------------------------------------------------------
    # GPU freeze detector
    # ------------------------------------------------------------------

    async def _gpu_freeze_detector(self) -> None:
        """Background coroutine: detect GPU freeze.

        Loops every 1.0s; if ``now - last_frame_at > 3.0`` seconds AND
        game state is ``running``, logs an error + sets the
        ``_gpu_freeze_detected`` flag + breaks out of the loop.

        The flag replaces raising ``CpuFreezeSuspected`` here: this
        task is started via ``asyncio.ensure_future`` and is never
        awaited at runtime — only during ``stop()`` /
        ``stop_gpu_stats_feed()`` cleanup — so a raised exception
        would be silently lost (asyncio's default handler logs it,
        but no caller could react to it). The flag is observable via
        ``was_gpu_freeze_detected()``.

        Does NOT trigger when state is ``paused`` (player paused, no
        frames expected) or other non-running states.
        """
        while True:
            try:
                await asyncio.sleep(1.0)
                if not self._gpu_stats_feed_enabled:
                    continue
                if self._last_frame_mono is None:
                    continue
                # Monotonic clock — immune to system time jumps (NTP
                # sync, manual adjust) that make wall-clock elapsed
                # values negative or huge.
                elapsed = time.monotonic() - self._last_frame_mono
                if elapsed > 3.0 and self.get_state() == "running":
                    logger.error(
                        "gpu freeze detected: no frame for >%.1fs while game running",
                        elapsed,
                        exc_info=True,
                    )
                    self._gpu_freeze_detected = True
                    # Stop the detector — freeze is reported once per
                    # feed session. Caller can query
                    # was_gpu_freeze_detected() and restart feed after
                    # diagnosis to re-arm detection.
                    break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("gpu_freeze_detector: error: %s", e)


class SteppingSubscription:
    """One subscriber's private view of ``cpu.stepping`` broadcasts.

    Created via ``GameStateObserver.subscribe_stepping()``; fed by the
    observer dispatcher from subscription time (broadcasts buffer even
    while nobody awaits — a hit that lands between arming a breakpoint
    and awaiting ``get()`` is not lost). Multiple concurrent
    subscriptions each receive every broadcast; none of them touches
    the shared step-confirmation queue.

    Queue discipline: bounded (``maxsize=32``), drop-oldest on overflow.
    A well-behaved subscriber consumes promptly; a stalled one loses
    only the OLDEST unobserved broadcasts instead of blocking the
    dispatcher (the dispatcher must never wait on a consumer).

    Lifecycle: ``close()`` (or ``async with``) detaches from the
    observer; idempotent. ``get()`` after close returns None on timeout
    as usual (the queue simply stops being fed).
    """

    def __init__(
        self,
        observer: GameStateObserver,
        maxsize: int = 32,
    ) -> None:
        self._observer = observer
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._closed = False

    def _offer(self, msg: dict[str, Any]) -> None:
        """Dispatcher hook: enqueue without ever blocking (never raises)."""
        if self._closed:
            return
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Drop the OLDEST broadcast to make room (bounded backlog).
            with contextlib.suppress(asyncio.QueueEmpty):  # pragma: no cover — race guard
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):  # pragma: no cover — race guard
                self._queue.put_nowait(msg)

    async def get(self, timeout_s: float) -> dict[str, Any] | None:
        """Await the next buffered broadcast.

        Returns:
            The broadcast dict, or None on timeout (pollable — a
            breakpoint-wait tool treats None as "not hit yet").
        """
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=max(0.0, timeout_s))
        except TimeoutError:
            return None

    def drain(self) -> int:
        """Drop all buffered broadcasts; returns how many were dropped.

        Used to discard stale broadcasts accumulated before the caller
        finished arming (same intent as ``GameStateObserver.drain_resume``).
        """
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                return dropped

    def close(self) -> None:
        """Detach from the observer (idempotent)."""
        if self._closed:
            return
        self._closed = True
        self._observer._remove_stepping_subscription(self)

    async def __aenter__(self) -> SteppingSubscription:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
