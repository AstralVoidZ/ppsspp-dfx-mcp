"""SteppingManager — safe stepping protocol manager for PPSSPP debugger.

Composes a WsTransport (or any object implementing the same call /
fire_and_forget / wait_for_state protocol) to provide transparent CPU
pause/resume for safe register and thread queries.

Key design choice: state is detected via `cpu.status.stepping`
(PPSSPP's stepping primitive), NOT `game.status.paused` (UI concept).
stepping is the PPSSPP pause primitive; paused is a UI state. This
eliminates the broadcast-queue confirmation race that the previous
`wait_stepping_broadcast()` approach suffered from.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from ppsspp_dfx_mcp.core.registers import extract_pc
from ppsspp_dfx_mcp.core.transport import WsTransport
from ppsspp_dfx_mcp.errors import SteppingFailedError

if TYPE_CHECKING:
    # Forward reference: GameStateObserver is implemented in task group 5
    # (core/game_state_observer.py). Use string annotation to avoid a
    # hard import that would create a circular dependency at runtime.
    from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver

logger = logging.getLogger(__name__)


# ---------- TrustLevel annotation ----------


class TrustLevel:
    """CPU/thread query result trust level.

    PPSSPP source marks "pc: inaccurate unless stepping" (CPUCoreSubscriber.cpp:105).
    isCurrent field is also only trustworthy when stepping (based on
    currentThread global; not updated when JIT/IR runs native code).

    All upper-layer APIs should use TrustLevel to decide whether to
    trust the return value:
    - HIGH: queried after stepping, source-guaranteed trustworthy
    - MEDIUM: memory variable query, not affected by CPU state
    - LOW: queried in RUNNING state, source-marked "inaccurate"

    Factory methods (from_pc_result / from_thread_result / high) preserve
    the current API (constants HIGH/MEDIUM/LOW) while providing a
    decision hook for future trust-level logic. The current safe_get_*
    methods always use with_stepping, so trust is HIGH; the factory
    methods encapsulate that decision so callers do not hardcode it.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @classmethod
    def high(cls) -> str:
        """Return HIGH trust level — used by safe_get_* methods which
        always pause CPU before querying."""
        return cls.HIGH

    @classmethod
    def from_pc_result(cls, result: dict[str, Any]) -> str:
        """Decide trust level for a PC query result.

        `result` is the cpu.getAllRegs response. PC is only trustworthy
        when CPU was stepping at query time. Since safe_get_pc always
        uses with_stepping, this returns HIGH unconditionally — the
        factory method exists to make the decision explicit and to
        provide a hook for future refinements (e.g. inspecting result
        metadata if PPSSPP ever exposes a "stepping" flag in the response).
        """
        return cls.HIGH

    @classmethod
    def from_thread_result(cls, result: dict[str, Any]) -> str:
        """Decide trust level for an hle.thread.list result.

        `result` is the hle.thread.list response. The isCurrent field
        is only trustworthy when stepping (PPSSPP HLESubscriber.cpp:65-100,
        based on currentThread global). Since safe_get_threads always
        uses with_stepping, this returns HIGH unconditionally.
        """
        return cls.HIGH


@dataclass(frozen=True)
class ThreadSnapshot:
    """Thread snapshot with trust annotation (frozen dataclass).

    Copied verbatim from the legacy monolithic PpssppClient to preserve
    the existing API. The `frozen=True` was added per the spec
    (stepping-manager/spec.md:38 "frozen dataclass") — the existing
    implementation was already effectively immutable in usage.
    """

    threads: list[dict]
    trust_level: str
    sampling_state: str
    timestamp: float = field(default_factory=time.time)


# ---------- Transport protocol (structural typing) ----------


class _TransportLike(Protocol):
    """Structural protocol that SteppingManager requires from its transport.

    WsTransport and the test FakeTransport both satisfy this protocol. Using a
    Protocol avoids hard-coupling SteppingManager to WsTransport, which
    is essential for testing with the test FakeTransport.
    """

    async def call(self, event: str, timeout: float = ..., **params: Any) -> dict[str, Any]: ...

    async def fire_and_forget(self, event: str, **params: Any) -> None: ...

    async def wait_for_state(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout_ms: int = ...,
        interval_ms: int = ...,
    ) -> dict[str, Any]: ...


# ---------- SteppingManager ----------


class SteppingManager:
    """Safe stepping protocol manager.

    Provides:
    - `with_stepping(preserve_state=True)` — async context manager that
      transparently pauses CPU on entry and conditionally resumes on exit.
    - `pause()` / `resume()` — standalone async helpers.
    - `safe_get_pc()` / `safe_get_threads()` — query with stepping for
      trustworthy results.

    The transport is injected (WsTransport in production, test FakeTransport
    in tests). SteppingManager does NOT own the transport — the caller
    (e.g. PpssppDebugClient) is responsible for its lifecycle.
    """

    def __init__(
        self,
        transport: WsTransport | _TransportLike,
        default_timeout_ms: int = 3000,
        default_interval_ms: int = 50,
        pid: int | None = None,
        game_state_observer: GameStateObserver | None = None,
    ) -> None:
        self._transport = transport
        self._default_timeout_ms = default_timeout_ms
        self._default_interval_ms = default_interval_ms
        # Decision 4/8: PID + observer for pause() PID pre-check and
        # resume() broadcast confirmation. Injected by
        # PpssppDebugClient.__init__ (which receives them from
        # SessionManager via client_helper). Both default to None so
        # legacy callers and unit tests can construct SteppingManager
        # with just a transport — pause()/resume() fall back to the
        # legacy conservative path when these are None.
        self._pid = pid
        self._game_state_observer = game_state_observer
        # Last cpu.status response seen (for pause() timeout error
        # message — encoding last ticks). Updated lazily by pause()
        # when it probes cpu.status before issuing cpu.stepping.
        self._last_status: dict[str, Any] | None = None

    # ---------- Pause / resume primitives ----------

    async def pause(self) -> dict[str, Any]:
        """Request CPU enter STEPPING state and confirm via wait_for_state.

        On ``wait_for_state`` timeout, performs a PID-alive pre-check
        (if ``self._pid`` is set) and raises ``SteppingFailedError``
        with the ``pid_alive`` attribute set. The PID pre-check
        distinguishes "PID dead = real disconnect" from "PID alive but
        CPU not entering STEPPING = suspected freeze". Both cases used
        to be uniformly classified as ``WsDisconnected`` by
        ``to_tool_error``, masking freeze scenarios.

        Non-``TimeoutError`` exceptions (e.g. ``ConnectionRefusedError``)
        are NOT caught here — they propagate to ``with_stepping``, which
        wraps them in ``SteppingFailedError`` (existing behavior for
        non-timeout exceptions).

        Returns:
            The cpu.status dict confirming stepping=True.
        """
        # Probe cpu.status first to capture last ticks for the error
        # message. Best-effort — failures here don't block pause().
        # Review issue #9: log full traceback at debug level so the
        # root cause (e.g. WS disconnect) is not lost behind a
        # bare exception message.
        try:
            self._last_status = await self._transport.call("cpu.status")
        except Exception as e:
            logger.debug(
                "pause: cpu.status probe failed (best-effort): %s",
                e,
                exc_info=True,
            )
            self._last_status = None

        await self._transport.fire_and_forget("cpu.stepping")
        try:
            return await self._transport.wait_for_state(
                lambda s: s.get("stepping") is True,
                timeout_ms=self._default_timeout_ms,
                interval_ms=self._default_interval_ms,
            )
        except TimeoutError as timeout_error:
            # PID pre-check (decision 4): distinguish "PID dead = real
            # disconnect" from "PID alive but CPU frozen". Only when
            # self._pid is set; otherwise raise without pid_alive.
            if self._pid is not None:
                from ppsspp_dfx_mcp.core import proc

                pid_alive = proc.is_pid_alive(self._pid)
                last_ticks = (
                    self._last_status.get("ticks") if self._last_status is not None else None
                )
                ticks_str = str(last_ticks) if last_ticks is not None else "unknown"
                if pid_alive:
                    msg = (
                        f"pause failed: PID alive but CPU not entering "
                        f"STEPPING; last ticks={ticks_str}"
                    )
                else:
                    msg = "pause failed: PID dead; PPSSPP process no longer running"
                raise SteppingFailedError(msg, pid_alive=pid_alive) from timeout_error
            # No PID context: raise without pid_alive attribute. The
            # default is pid_alive=None, so SteppingFailedError() will
            # fall back to __cause__-based translation in to_tool_error.
            raise SteppingFailedError(
                f"pause failed; CPU state unknown, cannot enter stepping context: {timeout_error}"
            ) from timeout_error

    async def resume(self) -> dict[str, Any]:
        """Request CPU resume and confirm via broadcast or polling.

        Decision 8 (broadcast confirmation path):
        - If ``self._game_state_observer`` is configured: send
          ``cpu.resume`` fire-and-forget, then call
          ``observer.wait_for_resume(timeout_ms=3000)``. If the
          broadcast confirms resume (returns True), return immediately
          without polling. If the broadcast times out (returns False),
          fall back to ``wait_for_state(stepping=False)`` polling.
        - If observer is None: existing behavior — send
          ``cpu.resume`` fire-and-forget, then poll
          ``wait_for_state(stepping=False)``.

        Returns:
            The cpu.status dict confirming stepping=False (when the
            polling fallback is invoked). When broadcast confirmation
            succeeds, returns an empty dict (no status polled).
        """
        # Drop stale cpu.resume broadcasts left over
        # from previous resume cycles BEFORE issuing the new command, so
        # wait_for_resume cannot be satisfied by a leftover broadcast
        # (0ms false confirmation) instead of this resume's own broadcast.
        if self._game_state_observer is not None:
            self._game_state_observer.drain_resume()
        await self._transport.fire_and_forget("cpu.resume")
        if self._game_state_observer is not None:
            ok = await self._game_state_observer.wait_for_resume(timeout_ms=3000)
            if ok:
                return {}
            # Broadcast timed out — fall back to polling.
            logger.debug(
                "resume: wait_for_resume broadcast timed out after "
                "3000ms — falling back to wait_for_state polling"
            )
        return await self._transport.wait_for_state(
            lambda s: s.get("stepping") is False,
            timeout_ms=self._default_timeout_ms,
            interval_ms=self._default_interval_ms,
        )

    # ---------- with_stepping context manager ----------

    @asynccontextmanager
    async def with_stepping(
        self,
        preserve_state: bool = True,
    ) -> AsyncIterator[None]:
        """Async context: pause CPU → execute block → conditionally resume.

        Args:
            preserve_state: if True (default), the CPU state prior to the
                block is preserved — if CPU was already stepping, it will
                NOT be resumed on exit. If False, CPU is always resumed
                on exit regardless of prior state.

        Behavior:
        - Records `was_stepping` via cpu.status (stepping=True means
          already paused).
        - If `preserve_state` is True and CPU was already stepping: yield
          without pausing, and do NOT resume after the block.
        - If CPU was running: pause via `pause()`, yield, then resume
          via `resume()` in the finally block.

        V020 fix (B.2 spec §3.2): the finally block distinguishes body
        success vs body exception. When the body succeeded, resume
        failures propagate to the caller (so callers know the CPU did
        not return to RUNNING). When the body raised, resume failures
        are logged as warnings but suppressed — the original body
        exception must propagate unchanged.
        """
        # Probe current stepping state via cpu.status.
        was_stepping = False
        try:
            status = await self._transport.call("cpu.status")
            was_stepping = bool(status.get("stepping", False))
        except Exception as e:
            # Conservative: assume not stepping (will pause + resume).
            # Log the failure so the root cause is not lost — a
            # cpu.status failure often indicates a disconnected
            # WebSocket, which will also cause pause() to fail.
            logger.warning(
                "with_stepping: cpu.status probe failed: %s; "
                "assuming CPU is running (will attempt pause)",
                e,
            )
            was_stepping = False

        # Decide whether to pause on entry.
        should_resume_on_exit = True
        if preserve_state and was_stepping:
            # Already stepping; preserve state — do not pause, do not resume.
            should_resume_on_exit = False
        else:
            # Either not stepping, or preserve_state=False → pause now.
            # If pause fails, raise immediately: continuing to execute the
            # body would violate the TrustLevel contract (safe_get_pc /
            # safe_get_threads would return data marked as HIGH trust
            # without the CPU actually being in stepping state).
            try:
                await self.pause()
            except SteppingFailedError:
                # pause() already constructed a SteppingFailedError with
                # the correct pid_alive attribute + __cause__ chain.
                # Propagate directly — do NOT double-wrap (would lose
                # pid_alive and mask the original __cause__ type).
                raise
            except Exception as e:
                # Non-TimeoutError exceptions (ConnectionRefusedError etc.)
                # are not caught by pause(); wrap them here so callers
                # only see SteppingFailedError from with_stepping.
                raise SteppingFailedError(
                    f"pause failed; CPU state unknown, cannot enter stepping context: {e}"
                ) from e

        body_succeeded = False
        try:
            yield
            body_succeeded = True
        finally:
            if should_resume_on_exit:
                if body_succeeded:
                    # Body succeeded — propagate resume failure so callers
                    # know the CPU did NOT return to RUNNING (V020 I11).
                    await self.resume()
                else:
                    # Body raised — record resume failure as a warning,
                    # but do NOT mask the original exception (V020 I12).
                    try:
                        await self.resume()
                    except Exception:
                        logger.warning(
                            "with_stepping: resume failed after body "
                            "exception; CPU may remain in STEPPING state",
                            exc_info=True,
                        )

    # ---------- Safe query primitives ----------

    async def safe_get_pc(self) -> tuple[int, str]:
        """Safe get PC — pause CPU, query cpu.getAllRegs, extract PC.

        Returns:
            (pc_value, trust_level) where trust_level is HIGH because
            CPU was stepping at query time (PPSSPP source guarantees PC
            accuracy when stepping).
        """
        async with self.with_stepping():
            regs = await self._transport.call("cpu.getAllRegs")
            pc = extract_pc(regs)
            trust = TrustLevel.from_pc_result(regs)
            return (pc, trust)

    async def safe_get_threads(self) -> ThreadSnapshot:
        """Safe get thread list — pause CPU, query hle.thread.list.

        Returns:
            ThreadSnapshot with trust_level=HIGH because CPU was
            stepping at query time (isCurrent is trustworthy when
            stepping).
        """
        async with self.with_stepping():
            resp = await self._transport.call("hle.thread.list")
            threads = resp.get("threads", [])
            trust = TrustLevel.from_thread_result(resp)
            return ThreadSnapshot(
                threads=threads,
                trust_level=trust,
                sampling_state="stepping",
            )

    # ---------- Helpers ----------
    # _extract_pc moved to core/registers.py (shared with PpssppDebugClient).
    # Use `extract_pc(regs)` directly.
