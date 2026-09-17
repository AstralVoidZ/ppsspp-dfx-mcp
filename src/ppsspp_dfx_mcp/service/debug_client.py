"""PpssppDebugClient — domain methods on top of WsTransport + SteppingManager.

Composes a WsTransport and creates a SteppingManager from it. Exposes
~50 domain methods organized by category. All WS event names (e.g.,
`memory.read_u32`, `cpu.getAllRegs`, `gpu.buffer.renderColor`) are
internal to this class — external callers use domain method names
(e.g., `read_u32`, `get_all_regs`, `render_color`).

Categories:
- Memory (9): read_u8/u16/u32/bytes/string, write_u8/u16/u32/bytes
- CPU (4): get_all_regs, get_pc, set_reg, evaluate
- Stepping delegation (3): with_stepping, pause, resume
- Stepping methods (5): step_into, step_over, step_out, run_until, next_hle
- Breakpoint (8): cpu_bp_add/remove/list/update, mem_bp_add/remove/list/update
- HLE (6): backtrace, module_list, func_list, func_scan, func_add, func_remove
- Disasm (3): disasm, assemble, search_disasm
- Input (3): press_button, hold_buttons, send_analog
- System (3): game_status, memory_map, reset
- GPU Buffer (3): render_color, texture, clut
- GPU Stats (1): gpu_stats
- GPU Record (1): gpu_record_dump
- Memory Info (1): search_memory_info
- Memory Scan (1): scan_memory (business orchestration, not WS event)
- Replay (8): replay_begin / replay_abort / replay_flush / replay_execute /
  replay_status / replay_time_get / replay_time_set / replay_wait_complete
  (replay_wait_complete is a client-side
  poller, not a WS event)

Note: `gpu.buffer.screenshot` is NOT exposed on DebugClient (crash risk).
Only CaptureService may use it via `self._client._transport.call()`.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal

from ppsspp_dfx_mcp.core.registers import normalize_reg_name
from ppsspp_dfx_mcp.core.stepping import SteppingManager, ThreadSnapshot
from ppsspp_dfx_mcp.core.transport import WsTransport
from ppsspp_dfx_mcp.core.ws_contract import get_contract
from ppsspp_dfx_mcp.errors import (
    CpuStateError,
    StepNoAdvanceError,
    StepOutError,
)

if TYPE_CHECKING:
    # Forward reference: GameStateObserver is implemented in task group 5
    # (core/game_state_observer.py). Use string annotation to avoid a
    # hard import that would create a circular dependency at runtime.
    from ppsspp_dfx_mcp.core.game_state_observer import GameStateObserver

logger = logging.getLogger(__name__)

# A2: PSP user-memory executable code range for step_out pc validation.
# top.prx loads at 0x08804000; HLE callbacks live in the same user-memory
# range. 0x08000000 is PSP RAM base (non-executable sentinel); values
# above 0x0C000000 are kernel/devkit memory (not reachable by step_out).
_PSP_CODE_RANGE_LOW = 0x08800000
_PSP_CODE_RANGE_HIGH = 0x0C000000

# Per-attempt broadcast window and no-op threshold
# for _step_with_retry — a no-op step emits its broadcast in well under
# a second, so a short window keeps the decisive failure fast.
_STEP_ATTEMPT_WINDOW_MS = 1200
_STEP_MAX_NO_ADVANCE = 3


def _press_button_timeout(duration: int) -> float:
    """Timeout for input.buttons.press, scaled to the press duration.

    PPSSPP presses immediately but only echoes the ticket after
    `duration` frames (~60 FPS), so long presses need a proportional
    budget: duration/60 s of hold time times 1.5 plus a 2 s margin,
    floored at the default 5 s transport timeout.
    """
    return max(5.0, duration / 60.0 * 1.5 + 2.0)


class PpssppDebugClient:
    """PPSSPP debug client — domain methods over WebSocket transport.

    Composes a WsTransport and a SteppingManager. The transport is
    injected (WsTransport in production, test FakeTransport in tests).
    PpssppDebugClient does NOT own the transport — the caller is
    responsible for its lifecycle (connect/close).

    Thread safety: single-coroutine use. Do not share across coroutines.
    """

    def __init__(
        self,
        transport: WsTransport,
        pid: int | None = None,
        game_state_observer: GameStateObserver | None = None,
    ) -> None:
        self._transport = transport
        # Keep the observer reference on the client so
        # step confirmation can route cpu.stepping broadcasts through the
        # observer's dedicated queue (see _wait_step_broadcast).
        self._game_state_observer = game_state_observer
        # Decision 4/8: forward pid + game_state_observer to
        # SteppingManager so pause() can do PID pre-check and resume()
        # can do broadcast confirmation. Both default to None for
        # backward compatibility with existing callers (and unit tests
        # that construct PpssppDebugClient with just a transport).
        self._stepping = SteppingManager(
            transport,
            pid=pid,
            game_state_observer=game_state_observer,
        )

    # ======================================================================
    # Stepping delegation (task 3.4)
    # ======================================================================

    @asynccontextmanager
    async def with_stepping(
        self,
        preserve_state: bool = True,
    ) -> AsyncIterator[None]:
        """Delegate to SteppingManager.with_stepping.

        Transparently pauses CPU on entry and conditionally resumes on
        exit. See SteppingManager.with_stepping for full semantics.
        """
        async with self._stepping.with_stepping(preserve_state=preserve_state):
            yield

    async def pause(self) -> dict[str, Any]:
        """Delegate to SteppingManager.pause."""
        return await self._stepping.pause()

    async def resume(self) -> dict[str, Any]:
        """Delegate to SteppingManager.resume."""
        return await self._stepping.resume()

    # ======================================================================
    # Memory methods (task 3.2) — 9 methods
    # ======================================================================

    async def read_u8(self, address: int) -> int:
        """Read unsigned 8-bit value at address."""
        resp = await self._transport.call("memory.read_u8", address=address)
        return int(resp.get("value", 0))

    async def read_u16(self, address: int) -> int:
        """Read unsigned 16-bit value at address."""
        resp = await self._transport.call("memory.read_u16", address=address)
        return int(resp.get("value", 0))

    async def read_u32(self, address: int) -> int:
        """Read unsigned 32-bit value at address."""
        resp = await self._transport.call("memory.read_u32", address=address)
        return int(resp.get("value", 0))

    async def read_bytes(self, address: int, size: int) -> bytes:
        """Read arbitrary-length memory region.

        PPSSPP `memory.read` response field is `base64` (not `data`).
        Falls back to `data` field for compatibility.
        """
        resp = await self._transport.call("memory.read", address=address, size=size)
        b64 = resp.get("base64", "")
        return base64.b64decode(b64) if b64 else b""

    async def read_string(
        self,
        address: int,
        encoding: Literal["utf-8", "base64"] = "utf-8",
        max_length: int = 4096,
    ) -> str:
        """Read a NUL-terminated string at address, capped client-side.

        PPSSPP's ``memory.readString`` uses
        ``strnlen`` up to the end of valid memory with NO length
        parameter (MemorySubscriber.cpp:L258-262) — reading a
        non-string region (e.g. code at 0x08804000) produces a
        multi-megabyte response that kills the WebSocket. We therefore
        never call that event: read at most ``max_length`` bytes via
        the bounded ``memory.read`` and cut at the first NUL locally.

        Args:
            address: memory address to read from.
            encoding: "utf-8" (default) or "base64" — "base64" here
                still returns the decoded byte content as a latin-1
                string for backwards compatibility with the tool view.
            max_length: byte cap (default 4096; hard ceiling 65536 — the
                same cap the tool layer advertises for ``max_len``).

        Returns:
            The decoded string (without the terminating NUL).
        """
        # Honor the tool layer's 64 KiB cap — the
        # previous hard 4096 here silently truncated max_len=65536 reads.
        # R9: ceiling imported from tools/_common — the W1 bug was this
        # literal drifting from the tool layer's copy.
        from ppsspp_dfx_mcp.core.primitives import MAX_SINGLE_READ_BYTES

        data = await self.read_bytes(address, max(1, min(max_length, MAX_SINGLE_READ_BYTES)))
        nul = data.find(b"\x00")
        raw = data if nul < 0 else data[:nul]
        if encoding == "base64":
            import base64 as _b64

            return _b64.b64encode(raw).decode("ascii")
        return raw.decode("utf-8", errors="replace")

    async def write_u8(self, address: int, value: int) -> None:
        """Write unsigned 8-bit value to address."""
        await self._transport.call("memory.write_u8", address=address, value=value)

    async def write_u16(self, address: int, value: int) -> None:
        """Write unsigned 16-bit value to address."""
        await self._transport.call("memory.write_u16", address=address, value=value)

    async def write_u32(self, address: int, value: int) -> None:
        """Write unsigned 32-bit value to address."""
        await self._transport.call("memory.write_u32", address=address, value=value)

    async def write_bytes(self, address: int, data: bytes) -> None:
        """Write arbitrary-length bytes to address (base64-encoded)."""
        b64 = base64.b64encode(data).decode("ascii")
        await self._transport.call("memory.write", address=address, base64=b64)

    # ======================================================================
    # CPU methods (task 3.3) — 4 methods
    # ======================================================================

    async def get_reg(self, name: str, thread: int | None = None) -> dict[str, Any]:
        """Read a single CPU register by name.

        Event: `cpu.getReg` (CPUCoreSubscriber.cpp:269-323) — `name` mode
        ("pc"/"hi"/"lo" special-cased by PPSSPP, otherwise the MIPS
        register name, e.g. "a0"/"v0"/"t9"). Response fields:
        {category, register, uintValue, floatValue}.

        Far cheaper than get_all_regs when one register is needed — the
        full dump carries three parallel arrays per category (hundreds of
        entries) while this returns one.
        """
        params: dict[str, Any] = {"name": normalize_reg_name(name)}
        if thread is not None:
            params["thread"] = thread
        return await self._transport.call("cpu.getReg", timeout=5.0, **params)

    async def get_all_regs(self, thread: int | None = None) -> dict[str, Any]:
        """Get all CPU registers (GPR + FPU + VFPU).

        Args:
            thread: Optional thread ID (u32). When provided, queries the
                register state for that thread instead of the current
                thread (see CPUCoreSubscriber.cpp:L34-37).
        """
        params: dict[str, Any] = {}
        if thread is not None:
            params["thread"] = thread
        return await self._transport.call("cpu.getAllRegs", timeout=5.0, **params)

    async def set_reg(
        self,
        name: str,
        value: int,
        thread: int | None = None,
    ) -> dict[str, Any]:
        """Set a CPU register value. Requires stepping state.

        DESTRUCTIVE: mutates CPU register state. Caller should ensure
        CPU is paused (e.g., via `with_stepping`) before calling.

        Args:
            name: Register name — MIPS ABI name ('a0'/'v0'/'t9'/'zero'...)
                or 'pc'/'hi'/'lo'. Numeric-style names ('r5') are
                translated to their ABI names automatically.
            value: Register value.
            thread: Optional thread ID (u32). When provided, sets the
                register for that thread instead of the current thread
                (see CPUCoreSubscriber.cpp:L34-37).
        """
        params: dict[str, Any] = {"name": normalize_reg_name(name), "value": value}
        if thread is not None:
            params["thread"] = thread
        async with self.with_stepping():
            return await self._transport.call("cpu.setReg", **params)

    async def evaluate(
        self,
        expression: str,
        thread: int | None = None,
    ) -> dict[str, Any]:
        """Evaluate a debugger expression (e.g., `r5 + 0x10`).

        Safe=True: uses with_stepping to ensure register values are
        trustworthy at evaluation time.

        Args:
            expression: Debugger expression to evaluate.
            thread: Optional thread ID (u32). When provided, evaluates
                the expression in the context of that thread instead of
                the current thread (see CPUCoreSubscriber.cpp:L34-37).
        """
        params: dict[str, Any] = {"expression": expression}
        if thread is not None:
            params["thread"] = thread
        async with self.with_stepping():
            return await self._transport.call("cpu.evaluate", **params)

    # ======================================================================
    # Stepping methods (task 3.5) — 5 methods
    # ======================================================================
    #
    # B2 (fire-and-forget confirmation): each step method pairs its
    # fire-and-forget call with a confirmation that the CPU re-entered
    # stepping state. Two confirmation modes are supported:
    #
    # - Broadcast mode (default, V004 redesign §2.4): subscribes to the
    #   `cpu.stepping` broadcast via `transport.wait_for_broadcast`.
    #   PPSSPP's SteppingBroadcaster pushes this event when the CPU
    #   enters CORE_STEPPING after a step (SteppingBroadcaster.cpp:L57-72).
    #   Returns the full broadcast dict (event/pc/ticks/reason/relatedAddress).
    #
    # - Legacy mode (fallback, V004 stage 2 single-poll): polls
    #   `cpu.status` via `wait_for_state(stepping is True)` with the
    #   full timeout budget. Used when `use_broadcast=False` or when
    #   broadcast mode times out (degraded path for old PPSSPP builds
    #   that may not push `cpu.stepping` broadcasts).
    #
    # Default timeout=5000ms, interval=200ms (legacy mode only) per the
    # fire-and-forget-confirmation spec. On timeout, raises `TimeoutError`.

    async def _wait_step_broadcast(
        self,
        timeout_ms: int,
        filter: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Consume one cpu.stepping broadcast via the
        routing-aware path.

        On session-level transports the GameStateObserver's single-consumer
        dispatcher owns ``transport.events`` — a raw
        ``transport.wait_for_broadcast('cpu.stepping')`` deterministically
        loses the race (the dispatcher is parked at the head of the getter
        queue) and every broadcast is consumed and silently dropped. When
        a running observer is attached, consume from its dedicated
        ``cpu.stepping`` queue instead; only fall back to the transport
        primitive when no live dispatcher exists (per-call fallback
        transports, unit tests with a bare FakeTransport).

        Raises:
            TimeoutError: no matching broadcast within ``timeout_ms``
                (mirrors ``wait_for_broadcast`` semantics so callers'
                existing fallback logic is unchanged).
        """
        observer = self._game_state_observer
        if observer is not None and observer.is_running():
            msg = await observer.wait_for_step_broadcast(timeout_ms=timeout_ms, filter=filter)
            if msg is None:
                raise TimeoutError(
                    "wait_for_step_broadcast timeout "
                    f"({timeout_ms}ms) — no matching 'cpu.stepping' broadcast"
                )
            return msg
        return await self._transport.wait_for_broadcast(
            event="cpu.stepping", timeout_ms=timeout_ms, filter=filter
        )

    async def _confirm_step_completed(
        self,
        timeout_ms: int = 5000,
        interval_ms: int = 200,  # noqa: ARG002 — legacy-mode only
        use_broadcast: bool = True,
        pre_pc: int | None = None,
        pre_ticks: float | None = None,
    ) -> dict[str, Any]:
        """Confirm a step completed by subscribing to `cpu.stepping` broadcast.

        V004 redesign (B.2 spec §2.4): replaces the legacy double-poll
        of `cpu.status` with a broadcast subscription. PPSSPP's
        SteppingBroadcaster pushes `cpu.stepping` events when the CPU
        enters CORE_STEPPING after a step (SteppingBroadcaster.cpp:L57-72).
        The broadcast carries `pc`/`ticks`/`reason`/`relatedAddress`
        fields — the legacy `cpu.status` poll only returned `stepping:
        bool`, losing the `reason` context.

        When `pre_pc`/`pre_ticks` are provided, a
        filter is applied to the broadcast subscription that rejects
        broadcasts whose `pc` AND `ticks` both equal the pre-step
        values. This excludes stale broadcasts produced by `pause()`
        (which shares the `cpu.stepping` event name with step commands)
        and prevents the "fake success" symptom where `step_into`
        returns the same pc/ticks as before the step. When both are
        `None` (default), no filter is applied — preserves backward
        compatibility for direct callers that don't track pre-step
        state (e.g. L4 regression tests).

        Args:
            timeout_ms: total timeout in milliseconds (default 5000).
            interval_ms: deprecated, kept for API compatibility. Only
                used by the legacy fallback path.
            use_broadcast: when True (default), subscribe to
                `cpu.stepping` broadcast via `transport.wait_for_broadcast`.
                When False, fall back to legacy `wait_for_state` polling.
            pre_pc: PC value before the step command was issued. When
                provided alongside `pre_ticks`, enables the stale-broadcast
                filter. ``None`` disables filtering (legacy behavior).
            pre_ticks: CPU tick count before the step command was issued.
                See `pre_pc` for filter semantics.

        Returns:
            In broadcast mode: the full `cpu.stepping` broadcast dict
            (event/pc/ticks/reason/relatedAddress fields).
            In legacy mode: the `cpu.status` dict confirming stepping=True.

        Raises:
            TimeoutError: step not confirmed within `timeout_ms` (both
                modes). In broadcast mode with the stale-broadcast
                filter active, a TimeoutError indicates the CPU did
                not advance (pc/ticks unchanged) — the caller should
                treat this as "step had no effect" rather than retry.
                In broadcast mode without the filter, a TimeoutError
                triggers a one-shot retry via the legacy path (degraded
                fallback for old PPSSPP builds that may not push
                broadcasts).
        """
        if use_broadcast:
            step_filter = self._build_step_filter(pre_pc, pre_ticks)
            try:
                return await self._wait_step_broadcast(timeout_ms=timeout_ms, filter=step_filter)
            except TimeoutError:
                # If the filter is active, do NOT fall back to legacy
                # poll — the legacy path has no pc/ticks filter and
                # would return the first stepping=True status, masking
                # the stale-broadcast problem we're trying to surface.
                if step_filter is not None:
                    raise
                # Degraded fallback: old PPSSPP builds may not push
                # cpu.stepping broadcasts. Fall through to legacy poll.
                logger.debug(
                    "wait_for_broadcast('cpu.stepping') timed out after "
                    "%dms — falling back to legacy wait_for_state poll",
                    timeout_ms,
                )
        return await self._confirm_step_completed_legacy(
            timeout_ms=timeout_ms, interval_ms=interval_ms
        )

    @staticmethod
    def _build_step_filter(
        pre_pc: int | None,
        pre_ticks: float | None,
    ) -> Callable[[dict[str, Any]], bool] | None:
        """A1: build a stale-broadcast filter for step confirmation.

        Returns None when neither pre_pc nor pre_ticks is available
        (caller didn't capture pre-step state) — disables filtering
        for backward compatibility. When at least one is provided,
        returns a filter that accepts broadcasts whose pc OR ticks
        differs from the pre-step value, rejecting stale broadcasts
        produced by `pause()` (which carry the same pc/ticks as the
        pre-step cpu.status).

        Strict semantics: when a field is None (unknown), it does NOT
        contribute to the OR — only known fields can flip the result
        to "accept". This favors false negatives (TimeoutError when
        the CPU genuinely advanced but we couldn't tell) over false
        positives (fake success consuming a stale pause broadcast),
        matching A1's design intent. So a broadcast whose only-known
        field matches the pre-step value is rejected regardless of
        the other field.

        Args:
            pre_pc: PC before step command (None if unknown).
            pre_ticks: ticks before step command (None if unknown).

        Returns:
            Filter callable or None.
        """
        if pre_pc is None and pre_ticks is None:
            return None

        def _filter(msg: dict[str, Any]) -> bool:
            pc = msg.get("pc")
            ticks = msg.get("ticks")
            # Each known field independently indicates change; unknown
            # fields are excluded from the OR (treated as "no change"),
            # so a stale broadcast whose only-known field matches the
            # pre-step value is rejected.
            pc_changed = pre_pc is not None and pc != pre_pc
            ticks_changed = pre_ticks is not None and ticks != pre_ticks
            return pc_changed or ticks_changed

        return _filter

    async def _confirm_step_completed_legacy(
        self,
        timeout_ms: int = 5000,
        interval_ms: int = 200,
    ) -> dict[str, Any]:
        """Legacy step confirmation: poll cpu.status until stepping=True.

        V004 stage 2 implementation (commit 778329e) — single
        `wait_for_state(stepping is True)` poll with the full timeout
        budget. Preserved as the fallback path for `_confirm_step_completed`
        when broadcast mode is disabled or times out (degraded path for
        old PPSSPP builds).

        Returns:
            The cpu.status dict confirming stepping=True.

        Raises:
            TimeoutError: stepping=True not confirmed within timeout_ms.
        """
        return await self._transport.wait_for_state(
            lambda s: s.get("stepping") is True,
            timeout_ms=timeout_ms,
            interval_ms=interval_ms,
        )

    async def _step_with_retry(
        self,
        event: str,
        timeout_ms: int,
        interval_ms: int,
        pre_pc: int | None,
        pre_ticks: float | None,
    ) -> dict[str, Any]:
        """Fire the step event and judge each
        cpu.stepping broadcast individually, re-firing on no-op steps.

        Real-PPSSPP finding (v1.20.4-605): cpu.stepInto /
        cpu.stepOver can be silent no-ops — a broadcast arrives with
        pc/ticks identical to the pre-step values (the CPU never
        advances) while the stepping counter still bumps. The plain
        ``_confirm_step_completed`` path cannot distinguish "no-op
        broadcast" from "no broadcast", so it burns the whole timeout
        and reports a generic TimeoutError. Here every broadcast is
        consumed and judged: advanced → return; unchanged → re-fire;
        after ``_STEP_MAX_NO_ADVANCE`` unchanged broadcasts → decisive
        SteppingFailedError with actionable semantics (~2s instead of
        a 5s blind timeout).

        Falls back to the legacy cpu.status poll only when PPSSPP
        pushed no broadcasts at all (old builds).
        """
        step_filter = self._build_step_filter(pre_pc, pre_ticks)
        deadline = time.monotonic() + timeout_ms / 1000.0
        no_advance = 0
        saw_broadcast = False
        # Fire the step command once up front; subsequent fires happen
        # only after a judged no-op broadcast.
        await self._transport.fire_and_forget(event)
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                break
            try:
                # NOTE: deliberately UNFILTERED — _step_with_retry judges
                # every broadcast itself (no-advance counting + re-fire),
                # so the stale-broadcast filter must NOT be pushed down.
                msg = await self._wait_step_broadcast(
                    timeout_ms=min(_STEP_ATTEMPT_WINDOW_MS, remaining_ms),
                )
            except TimeoutError:
                continue
            saw_broadcast = True
            if step_filter is None or step_filter(msg):
                return msg
            no_advance += 1
            if no_advance >= _STEP_MAX_NO_ADVANCE:
                # W10b fix: StepNoAdvanceError (a ToolError) instead of
                # SteppingFailedError — the no-advance diagnosis used to be
                # misclassified as WsDisconnected with a misleading
                # "reconnect" hint appended.
                raise StepNoAdvanceError(
                    "step did not advance the CPU after "
                    f"{no_advance} attempt(s) (pc/ticks unchanged) — "
                    "PPSSPP is not consuming steps at this state; use a "
                    "breakpoint or step action=run_until instead"
                )
            await self._transport.fire_and_forget(event)
        if not saw_broadcast:
            return await self._confirm_step_completed_legacy(
                timeout_ms=timeout_ms, interval_ms=interval_ms
            )
        raise TimeoutError(f"step not confirmed within {timeout_ms}ms")

    async def step_into(
        self,
        timeout_ms: int = 5000,
        interval_ms: int = 200,
    ) -> dict[str, Any]:
        """Step into (including delay slot).

        Ensures the CPU is stepping before issuing
        cpu.stepInto (REQUIRED_STEPPING precondition — SteppingSubscriber
        .cpp:L140-141 rejects stepOver/stepOut when running, and stepInto
        when running silently produces no broadcast), captures
        pre_pc/pre_ticks, and applies the stale-broadcast filter to
        exclude the `cpu.stepping` broadcast produced by pause().

        Raises:
            TimeoutError: step not confirmed within `timeout_ms`, OR
                the stale-broadcast filter rejected all broadcasts (CPU
                did not advance — pc/ticks unchanged). The latter is the
                correct behavior for a stalled CPU (e.g. waiting for
                interrupt): callers previously got a "fake success"
                consuming a stale pause broadcast; now they get a
                TimeoutError they can act on.
        """
        pre_pc, pre_ticks = await self._prepare_for_step()
        return await self._step_with_retry(
            "cpu.stepInto",
            timeout_ms=timeout_ms,
            interval_ms=interval_ms,
            pre_pc=pre_pc,
            pre_ticks=pre_ticks,
        )

    async def step_over(
        self,
        timeout_ms: int = 5000,
        interval_ms: int = 200,
    ) -> dict[str, Any]:
        """Step over (skip function calls).

        Ensures the CPU is stepping before issuing
        cpu.stepOver (REQUIRED_STEPPING precondition — without this,
        PPSSPP returns a ticketed error via fire_and_forget that is
        never matched by wait_for_broadcast, causing a 6125ms double
        timeout). Captures pre_pc/pre_ticks and applies the stale-
        broadcast filter.

        Raises:
            TimeoutError: step not confirmed within `timeout_ms`, OR
                the stale-broadcast filter rejected all broadcasts.
        """
        pre_pc, pre_ticks = await self._prepare_for_step()
        return await self._step_with_retry(
            "cpu.stepOver",
            timeout_ms=timeout_ms,
            interval_ms=interval_ms,
            pre_pc=pre_pc,
            pre_ticks=pre_ticks,
        )

    async def step_out(
        self,
        timeout_ms: int = 5000,
        interval_ms: int = 200,
    ) -> dict[str, Any]:
        """Step out of current function.

        Step with pre-state capture:
        - A1: ensures CPU is stepping before issuing cpu.stepOut
          (REQUIRED_STEPPING precondition).
        - A2: post-validates the returned pc against the PSP executable
          code range (``0x08800000``–``0x0C000000``). When step_out's
          stack walk returns an invalid caller frame (e.g.
          ``0x08000000`` PSP user-memory base address used as a
          stack-bottom sentinel), the broadcast consumed is a stale
          one and the returned pc is non-executable — raise
          ``StepOutError`` so callers can distinguish "stepped out to
          invalid location" from "step_out timed out".

        Raises:
            TimeoutError: step not confirmed within `timeout_ms`, OR
                stale-broadcast filter rejected all broadcasts.
            StepOutError: step completed but returned pc is outside
                the PSP executable code range — likely a stale
                broadcast from pause()/step_over() rather than the
                actual step_out completion.
        """
        pre_pc, pre_ticks = await self._prepare_for_step()
        result = await self._step_with_retry(
            "cpu.stepOut",
            timeout_ms=timeout_ms,
            interval_ms=interval_ms,
            pre_pc=pre_pc,
            pre_ticks=pre_ticks,
        )
        self._validate_step_out_pc(result)
        return result

    async def _prepare_for_step(self) -> tuple[int | None, float | None]:
        """Ensure CPU stepping + capture pre-step pc/ticks.

        If the CPU is running, calls pause() to enter stepping state
        (REQUIRED_STEPPING precondition for stepOver/stepOut, and to
        avoid stepInto's silent no-broadcast path when running). Then
        queries cpu.status to capture pre_pc/pre_ticks for the stale-
        broadcast filter.

        Returns:
            (pre_pc, pre_ticks) — both None if cpu.status did not
            include the fields (e.g. test FakeTransport with minimal
            state), which disables the filter via _build_step_filter.
        """
        status = await self._transport.call("cpu.status")
        if not status.get("stepping"):
            await self._stepping.pause()
            status = await self._transport.call("cpu.status")
        pre_pc = status.get("pc")
        pre_ticks = status.get("ticks")
        return (
            int(pre_pc) if pre_pc is not None else None,
            float(pre_ticks) if pre_ticks is not None else None,
        )

    @staticmethod
    def _validate_step_out_pc(result: dict[str, Any]) -> None:
        """A2: post-validate step_out returned pc against PSP code range.

        PSP user-memory executable range is 0x08800000–0x0C000000
        (top.prx loads at 0x08804000; HLE/kernel code lives above
        0x0C000000). A returned pc of 0x08000000 (PSP user-memory base
        address) indicates the stack walk returned a stack-bottom
        sentinel rather than a real caller frame, and the consumed
        broadcast was a stale one.

        Raises:
            StepOutError: if pc is outside the executable range.
        """
        pc = result.get("pc")
        if pc is None:
            # No pc field — can't validate, let caller handle.
            return
        try:
            pc_int = int(pc)
        except (TypeError, ValueError):
            return
        if not (_PSP_CODE_RANGE_LOW <= pc_int <= _PSP_CODE_RANGE_HIGH):
            raise StepOutError(
                f"step_out returned pc=0x{pc_int:08X} which is outside "
                f"the PSP executable code range "
                f"(0x{_PSP_CODE_RANGE_LOW:08X}–0x{_PSP_CODE_RANGE_HIGH:08X}). "
                f"This typically indicates the stack walk returned an "
                f"invalid caller frame (e.g. stack-bottom sentinel "
                f"0x08000000) and the consumed cpu.stepping broadcast "
                f"was a stale one. Retry step_out after pausing, or "
                f"step_into/step_over to advance past the current frame."
            )

    async def run_until(
        self,
        address: int,
        timeout_ms: int = 5000,
        interval_ms: int = 200,
    ) -> dict[str, Any]:
        """Run until the specified address is reached.

        Fire-and-forget + confirmation: sends cpu.runUntil, then polls
        cpu.status until stepping=True (address reached, CPU paused).
        Raises TimeoutError if the address is not reached within timeout_ms
        (which is expected when the address is never hit).

        A1 fix: captures pre_pc/pre_ticks before issuing the command to
        filter stale cpu.stepping broadcasts (same pattern as step_into/
        step_over/step_out).
        """
        pre_pc, pre_ticks = await self._prepare_for_step()
        await self._transport.fire_and_forget("cpu.runUntil", address=address)
        return await self._confirm_step_completed(
            timeout_ms=timeout_ms,
            interval_ms=interval_ms,
            pre_pc=pre_pc,
            pre_ticks=pre_ticks,
        )

    async def next_hle(
        self,
        timeout_ms: int = 5000,
        interval_ms: int = 200,
    ) -> dict[str, Any]:
        """Step to next HLE callback.

        Fire-and-forget + confirmation: sends cpu.nextHLE, then polls
        cpu.status until stepping=True (HLE callback reached). Raises
        TimeoutError if no HLE callback is hit within timeout_ms.

        A1 fix: captures pre_pc/pre_ticks before issuing the command to
        filter stale cpu.stepping broadcasts (same pattern as step_into/
        step_over/step_out).
        """
        pre_pc, pre_ticks = await self._prepare_for_step()
        await self._transport.fire_and_forget("cpu.nextHLE")
        return await self._confirm_step_completed(
            timeout_ms=timeout_ms,
            interval_ms=interval_ms,
            pre_pc=pre_pc,
            pre_ticks=pre_ticks,
        )

    # ======================================================================
    # Breakpoint methods (task 3.6) — 8 methods
    # ======================================================================

    async def cpu_bp_add(
        self,
        address: int,
        enabled: bool = True,
        condition: str | None = None,
        log: bool | None = None,
        log_format: str | None = None,
    ) -> dict[str, Any]:
        """Add a CPU execution breakpoint.

        Args:
            address: Breakpoint address.
            enabled: Whether the breakpoint is enabled (default True).
            condition: Optional break condition expression (e.g.,
                'a0==1'). When None, no condition is sent (PPSSPP
                treats the breakpoint as unconditional).
            log: Optional bool. When True, PPSSPP logs each breakpoint
                hit (see BreakpointSubscriber.cpp:L28, L136-144).
            log_format: Optional log format string (e.g., 'hit @ {pc}').
                Forwarded as `logFormat` (camelCase). When None, no
                logFormat is sent.
        """
        params: dict[str, Any] = {"address": address, "enabled": enabled}
        if condition is not None:
            params["condition"] = condition
        if log is not None:
            params["log"] = log
        if log_format is not None:
            params["logFormat"] = log_format
        return await self._transport.call("cpu.breakpoint.add", **params)

    async def cpu_bp_remove(self, address: int) -> dict[str, Any]:
        """Remove a CPU execution breakpoint."""
        return await self._transport.call("cpu.breakpoint.remove", address=address)

    async def cpu_bp_list(self) -> dict[str, Any]:
        """List all CPU breakpoints."""
        return await self._transport.call("cpu.breakpoint.list")

    async def cpu_bp_update(
        self,
        address: int,
        enabled: bool | None = None,
        log: bool | None = None,
        condition: str | None = None,
        log_format: str | None = None,
    ) -> dict[str, Any]:
        """Update CPU breakpoint attributes."""
        params: dict[str, Any] = {"address": address}
        if enabled is not None:
            params["enabled"] = enabled
        if log is not None:
            params["log"] = log
        if condition is not None:
            params["condition"] = condition
        if log_format is not None:
            params["logFormat"] = log_format
        return await self._transport.call("cpu.breakpoint.update", **params)

    async def mem_bp_add(
        self,
        address: int,
        size: int = 4,
        read: bool = True,
        write: bool = True,
        change: bool = False,
        enabled: bool = True,
        log: bool = False,
        condition: str | None = None,
        log_format: str | None = None,
    ) -> dict[str, Any]:
        """Add a memory access breakpoint.

        `read`, `write`, and `change` are sent as separate boolean params
        to PPSSPP (see BreakpointSubscriber.cpp — `read`/`write`/`change`
        are independent OPTIONAL bools). `change` tracks modifications
        (write-with-same-value is ignored by PPSSPP when `change=True`).

        V018: read/write/change are ALWAYS sent (not false-omission),
        matching the `enabled` style. This lets callers explicitly
        override PPSSPP defaults (e.g. send `read: False` to disable).
        """
        params: dict[str, Any] = {
            "address": address,
            "size": size,
            "enabled": enabled,
            # V018: always send read/write/change (not false-omission).
            # See BreakpointSubscriber.cpp:L286 — these are independent
            # OPTIONAL bools; sending them explicitly lets callers
            # override PPSSPP defaults rather than relying on absence.
            "read": read,
            "write": write,
            "change": change,
            # N-02: always send log (not false-omission). See
            # BreakpointSubscriber.cpp:L309-325 Result(bool) — when log
            # is omitted (hasLog=false), the `log` C++ variable is
            # uninitialized and Result(true) reads UB. Sending log
            # explicitly (True or False) avoids the UB and makes the
            # returned log field consistent with the input.
            "log": log,
        }
        if condition is not None:
            params["condition"] = condition
        if log_format is not None:
            params["logFormat"] = log_format
        return await self._transport.call("memory.breakpoint.add", **params)

    async def mem_bp_remove(self, address: int, size: int) -> dict[str, Any]:
        """Remove a memory access breakpoint.

        Args:
            address: Breakpoint address.
            size: Breakpoint watch size in bytes (required by PPSSPP contract;
                memory breakpoints are matched by address+size pair).
        """
        return await self._transport.call("memory.breakpoint.remove", address=address, size=size)

    async def mem_bp_list(self) -> dict[str, Any]:
        """List all memory breakpoints."""
        return await self._transport.call("memory.breakpoint.list")

    async def mem_bp_update(
        self,
        address: int,
        size: int,
        enabled: bool | None = None,
        log: bool | None = None,
        condition: str | None = None,
        log_format: str | None = None,
        read: bool | None = None,
        write: bool | None = None,
        change: bool | None = None,
    ) -> dict[str, Any]:
        """Update memory breakpoint attributes.

        Args:
            address: Breakpoint address.
            size: Breakpoint watch size in bytes (required by PPSSPP contract;
                memory breakpoints are matched by address+size pair).
            enabled: Optional new enabled flag.
            log: Optional new log flag.
            condition: Optional new condition expression.
            log_format: Optional new log format string.
            read: Optional new read flag (V018). When provided, forwarded
                as an explicit bool (see BreakpointSubscriber.cpp:L286).
            write: Optional new write flag (V018).
            change: Optional new change flag (V018).
        """
        params: dict[str, Any] = {"address": address, "size": size}
        if enabled is not None:
            params["enabled"] = enabled
        if log is not None:
            params["log"] = log
        if condition is not None:
            params["condition"] = condition
        if log_format is not None:
            params["logFormat"] = log_format
        # V018: read/write/change are optional partial-update fields —
        # only forwarded when the caller provides them (None = no change).
        # This differs from mem_bp_add (which always sends them) because
        # update is patchy by nature.
        if read is not None:
            params["read"] = read
        if write is not None:
            params["write"] = write
        if change is not None:
            params["change"] = change
        return await self._transport.call("memory.breakpoint.update", **params)

    async def backtrace(self, thread: int | None = None) -> dict[str, Any]:
        """Get call stack backtrace. If `thread` is None, uses current.

        REQUIRED_STEPPING: PPSSPP rejects hle.backtrace when CPU is running
        (HLESubscriber.cpp:557-559). with_stepping transparently pauses
        the CPU before the call and resumes after.
        """
        params: dict[str, Any] = {}
        if thread is not None:
            params["thread"] = thread
        async with self.with_stepping():
            return await self._transport.call("hle.backtrace", **params)

    async def module_list(self) -> dict[str, Any]:
        """List all loaded HLE modules."""
        return await self._transport.call("hle.module.list", timeout=5.0)

    async def func_list(self) -> dict[str, Any]:
        """List registered HLE function tracking entries."""
        return await self._transport.call("hle.func.list", timeout=5.0)

    async def func_scan(
        self,
        address: int,
        size: int,
        remove: bool | None = None,
    ) -> dict[str, Any]:
        """Scan a memory range for trackable HLE functions.

        REQUIRED_STEPPING: PPSSPP rejects func.scan when CPU is running
        (HLESubscriber.cpp:486-488). with_stepping transparently pauses
        the CPU before the call and resumes after.

        Args:
            address: start address of the scan range.
            size: size in bytes of the scan range.
            remove: Optional bool. When True, PPSSPP clears existing
                functions in the range before scanning (see
                HLESubscriber.cpp:L41, L483-511).
        """
        params: dict[str, Any] = {"address": address, "size": size}
        if remove is not None:
            params["remove"] = remove
        async with self.with_stepping():
            return await self._transport.call("hle.func.scan", timeout=10.0, **params)

    async def func_add(
        self,
        name: str | None = None,
        address: int | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        """Add HLE function tracking. Specify `name` and/or `address`.

        REQUIRED_STEPPING: PPSSPP rejects func.add when CPU is running
        (HLESubscriber.cpp:243-245). with_stepping transparently pauses
        the CPU before the call and resumes after.

        Args:
            name: Optional function name.
            address: Optional function address.
            size: Optional function size in bytes. When None, PPSSPP
                infers from the previous function or defaults to 4
                (see HLESubscriber.cpp:L37, L240-307).
        """
        params: dict[str, Any] = {}
        if name is not None:
            params["name"] = name
        if address is not None:
            params["address"] = address
        if size is not None:
            params["size"] = size
        async with self.with_stepping():
            return await self._transport.call("hle.func.add", **params)

    async def func_remove(self, address: int) -> dict[str, Any]:
        """Remove HLE function tracking at `address` (required).

        REQUIRED_STEPPING: PPSSPP rejects func.remove when CPU is running
        (HLESubscriber.cpp:322-324). with_stepping transparently pauses
        the CPU before the call and resumes after.

        See PPSSPP HLESubscriber.cpp:L38, L319-363 — only `address` is
        registered; the contract has no `name` parameter.
        """
        async with self.with_stepping():
            return await self._transport.call("hle.func.remove", address=address)

    # ======================================================================
    # Disasm methods (task 3.8) — 3 methods
    # ======================================================================

    async def disasm(
        self,
        address: int,
        count: int = 10,
        thread: int | None = None,
    ) -> list[dict]:
        """Disassemble `count` instructions at `address`.

        Each returned dict has at minimum a `text` field (the assembly
        text). If PPSSPP returns separate `name`/`params` fields, they
        are concatenated into `text`.

        Args:
            address: starting address for disassembly.
            count: number of instructions to disassemble (default 10).
            thread: Optional thread ID (u32). When provided, disassembles
                in the context of that thread (see DisasmSubscriber.cpp:L58-59).
        """
        params: dict[str, Any] = {"address": address, "count": count}
        if thread is not None:
            params["thread"] = thread
        resp = await self._transport.call("memory.disasm", **params)
        lines = resp.get("lines", [])
        for line in lines:
            if "text" not in line:
                name = line.get("name", "")
                line_params = line.get("params", "")
                line["text"] = f"{name} {line_params}".strip() if line_params else name
        return lines

    async def assemble(self, address: int, code: str) -> dict[str, Any]:
        """Assemble MIPS instruction(s) at address.

        DESTRUCTIVE: writes assembled bytes to memory. Caller should
        ensure this is intended.
        """
        return await self._transport.call("memory.assemble", address=address, code=code)

    async def search_disasm(
        self,
        address: int,
        match: str,
        end: int = 0,
        display_symbols: bool = True,
        thread: int | None = None,
    ) -> dict[str, Any]:
        """Search disassembly for instructions matching `match`. READ-ONLY.

        Args:
            address: starting address for the search.
            match: case-insensitive substring to find in the disassembly
                text (e.g., 'jal', 'addiu', 'lw r5'). Required by
                PPSSPP's `memory.searchDisasm` event.
            end: end address; if 0 or <= address, PPSSPP performs a
                loop search (wraps around memory).
            display_symbols: if True, render symbol names in the output.
            thread: Optional thread ID (u32). When provided, searches in
                the context of that thread (see DisasmSubscriber.cpp:L58-59).
        """
        params: dict[str, Any] = {
            "address": address,
            "match": match,
            "displaySymbols": display_symbols,
        }
        if end > address:
            params["end"] = end
        if thread is not None:
            params["thread"] = thread
        return await self._transport.call("memory.searchDisasm", **params)

    # ======================================================================
    # Input methods (task 3.9) — 3 methods
    # ======================================================================

    async def press_button(self, button: str, duration: int = 1) -> dict[str, Any]:
        """Simulate button press for `duration` frames.

        Buttons: cross/circle/triangle/square/up/down/left/right/
        start/select/ltrigger/rtrigger (plus the console-only keys
        accepted by PPSSPP's buttonLookup table).

        Default duration=1 matches PPSSPP `input.buttons.press` contract
        (InputSubscriber.cpp:L93, L170-192). The event is asynchronous:
        PPSSPP releases the button after `duration` frames and only then
        echoes the ticket, so the timeout must scale with duration
        (~60 FPS) or long presses time out spuriously.
        """
        timeout = _press_button_timeout(duration)
        return await self._transport.call(
            "input.buttons.press", timeout=timeout, button=button, duration=duration
        )

    async def hold_buttons(self, buttons: dict[str, bool]) -> dict[str, Any]:
        """Hold specified buttons until released.

        Args:
            buttons: dict mapping button name → press state (True=held,
                False=released). Example: {"cross": True, "circle": True}.

        STATE-CHANGE: changes the persistent button state.
        """
        return await self._transport.call("input.buttons.send", buttons=buttons)

    async def send_analog(
        self,
        x: float,
        y: float,
        stick: str = "left",
    ) -> dict[str, Any]:
        """Send analog stick position.

        Args:
            x, y: analog stick coordinates in [-1.0, 1.0] (PPSSPP native
                range; 0.0 = center).
            stick: which analog stick to set — "left" or "right"
                (default "left"; see InputSubscriber.cpp:L94, L242-256).
        """
        return await self._transport.call("input.analog.send", x=x, y=y, stick=stick)

    # ======================================================================
    # System methods (task 3.10) — 4 methods
    # ======================================================================

    async def game_status(self) -> dict[str, Any]:
        """Query game status (running/paused, game title, etc.)."""
        return await self._transport.call("game.status", timeout=5.0)

    async def memory_map(self) -> dict[str, Any]:
        """Get memory region map.

        Uses `memory.mapping` (no parameters) — returns a `ranges` array
        of memory regions (ram / vram / sram, primary / mirror). The
        previous implementation used `memory.info.list`, which requires
        `address` + `size` parameters and is intended for querying
        memory-info tags within a range, not for listing regions.
        """
        return await self._transport.call("memory.mapping")

    async def reset(self, break_: bool | None = None) -> dict[str, Any]:
        """Reset the game (reboot).

        DESTRUCTIVE: causes game state loss.

        Args:
            break_: Optional bool (WS name: `break`). When True, sets
                startBreak after reset (see GameSubscriber.cpp:L26,
                L41-62). Python `break` is a keyword, so the param is
                named `break_`; forwarded as `break` to PPSSPP.
        """
        params: dict[str, Any] = {}
        if break_ is not None:
            params["break"] = break_
        return await self._transport.call("game.reset", **params)

    async def render_color(
        self,
        output_type: Literal["uri", "base64"] = "uri",
        alpha: bool = False,
        stackWidth: int = 0,
    ) -> dict[str, Any]:
        """Capture GPU render color buffer.

        Args:
            output_type: output encoding ("uri" returns data URI with PNG,
                "base64" returns raw pixels). Default "uri" — PPSSPP does
                format conversion + PNG encoding internally.
            alpha: include alpha channel for "uri" type. Default False.
            stackWidth: force width for "uri" type (0 = native). Default 0.
        """
        return await self._transport.call(
            "gpu.buffer.renderColor",
            type=output_type,
            alpha=alpha,
            stackWidth=stackWidth,
        )

    async def texture(
        self,
        level: int = 0,
        output_type: Literal["uri", "base64"] = "uri",
        alpha: bool = False,
        stackWidth: int = 0,
    ) -> dict[str, Any]:
        """Capture the currently-bound GPU texture.

        PPSSPP captures the texture currently bound to the GE state — it
        does NOT support capturing by VRAM address. The `level` parameter
        selects the mipmap level. See PPSSPP `GPUBufferSubscriber.cpp:L378-
        386` (`WebSocketGPUBufferTexture` → `GPU_GetCurrentTexture`).

        Args:
            level: mipmap level (default 0).
            output_type: output encoding ("uri" or "base64"). Default "uri".
            alpha: include alpha channel for "uri" type. Default False.
            stackWidth: force width for "uri" type (0 = native). Default 0.
        """
        return await self._transport.call(
            "gpu.buffer.texture",
            level=level,
            type=output_type,
            alpha=alpha,
            stackWidth=stackWidth,
            timeout=15.0,
        )

    async def render_depth(
        self,
        output_type: Literal["uri", "base64"] = "uri",
        alpha: bool = False,
        stackWidth: int = 0,
    ) -> dict[str, Any]:
        """Capture the GPU depth buffer.

        This method was documented as the target of
        ``CaptureService.dump_buffer`` but never implemented — depth/stencil
        dumps always died on AttributeError and were masked as "Unsupported
        dump_buffer target". Contracts: gpu.buffer.renderDepth
        (REQUIRED_STEPPING_OR_GPU_STEPPING, ws_contract).
        """
        return await self._transport.call(
            "gpu.buffer.renderDepth",
            type=output_type,
            alpha=alpha,
            stackWidth=stackWidth,
        )

    async def render_stencil(
        self,
        output_type: Literal["uri", "base64"] = "uri",
        alpha: bool = False,
        stackWidth: int = 0,
    ) -> dict[str, Any]:
        """Capture the GPU stencil buffer. See render_depth."""
        return await self._transport.call(
            "gpu.buffer.renderStencil",
            type=output_type,
            alpha=alpha,
            stackWidth=stackWidth,
        )

    async def clut(
        self,
        output_type: Literal["uri", "base64"] = "uri",
        alpha: bool = False,
        stackWidth: int = 0,
    ) -> dict[str, Any]:
        """Capture the currently-bound GPU CLUT (palette).

        PPSSPP captures the CLUT currently bound to the GE state — it
        does NOT support capturing by VRAM address. See PPSSPP
        `GPUBufferSubscriber.cpp:L406-412` (`WebSocketGPUBufferClut` →
        `GPU_GetCurrentClut`).

        Args:
            output_type: output encoding ("uri" or "base64"). Default "uri".
            alpha: include alpha channel for "uri" type. Default False.
            stackWidth: force width for "uri" type (0 = native). Default 0.
        """
        return await self._transport.call(
            "gpu.buffer.clut",
            type=output_type,
            alpha=alpha,
            stackWidth=stackWidth,
        )

    # ======================================================================
    # GPU Stats methods (C-G1) — 1 method
    # ======================================================================
    #
    # D3 (gpu events): `gpu.stats.get` is an async ticketed event — PPSSPP
    # pushes stats on the next GPU flip. Use `transport.call()` with a
    # ticket wait. Precondition: game must be running (CPU not stepping)
    # for a flip to occur within the timeout; if CPU is paused, the call
    # will timeout. Default timeout=5.0s is generous (60fps → ~16ms/frame).
    #
    # Batch 1 (REQUIRED_RUNNING pre-check): before issuing the ticketed
    # call, DebugClient probes `cpu.status`. If stepping=True, raises
    # CpuStateError with the contract's diagnostic_hint instead of
    # waiting for the silent timeout. This converts a 5-second hang into
    # an immediate actionable error.

    async def _require_running(self, event: str) -> None:
        """Pre-check CPU state for a REQUIRED_RUNNING event.

        Performs ticks progression detection as a **breakpoint pause
        identification** mechanism, cross-validated with the ``stepping``
        field of ``cpu.status``:

        1. First ``cpu.status`` call: record ``ticks0`` + ``stepping0``.
        2. ``await asyncio.sleep(0.05)`` (50ms).
        3. Second ``cpu.status`` call: record ``ticks1`` + ``stepping1``.
        4. Decision matrix:
           - ``stepping0=True`` OR ``stepping1=True`` → raise
             ``CpuStateError`` (existing behavior: stepping field
             detected pause).
           - ``stepping0=False`` AND ``stepping1=False`` AND
             ``ticks0``/``ticks1`` both present AND
             ``ticks0 == ticks1`` → raise ``CpuStateError`` (ticks
             backup validation: breakpoint pause suspected but stepping
             field didn't report).
           - ``stepping0=False`` AND ``stepping1=False`` AND
             (``ticks0`` or ``ticks1`` is None) → skip ticks check
             (ticks not available, rely on stepping field only),
             return.
           - ``stepping0=False`` AND ``stepping1=False`` AND
             ``ticks0 != ticks1`` → CPU progressing normally, return
             (allow ticketed call to proceed).

        Critical scope (decision 4, see cpu-freeze-detection/spec.md):
        ticks detection is **only** for breakpoint pause
        (``CORE_STEPPING``) identification. It does NOT detect CPU死循环
        (ticks still increase) or HLE阻塞 (ticks still increase). For
        those scenarios, ``to_tool_error``'s ``TimeoutError``
        comprehensive judgment (decision 9) handles them via PID +
        game-state综合判定.

        Args:
            event: WS event name (must have a contract entry).

        Raises:
            CpuStateError: if CPU is currently stepping OR if ticks
                unchanged after 50ms (breakpoint pause suspected).
        """
        # First probe — record ticks0 + stepping0.
        try:
            status0 = await self._transport.call("cpu.status")
        except (ConnectionRefusedError, OSError):
            # Transport-level failure (WS disconnected / port unreachable):
            # re-raise so to_tool_error can classify it as WsDisconnected
            # immediately, rather than letting the ticketed call time out.
            raise
        except Exception:
            # Other probe failures (e.g. PPSSPP internal error): fall
            # through and let the ticketed call surface the underlying error.
            return
        stepping0 = status0.get("stepping") is True
        ticks0 = status0.get("ticks")

        # 50ms sleep — long enough for ticks to advance on a running
        # CPU (60fps → ~16ms/frame → ~3 frames in 50ms), short enough
        # to keep the pre-check cost negligible.
        await asyncio.sleep(0.05)

        # Second probe — record ticks1 + stepping1.
        try:
            status1 = await self._transport.call("cpu.status")
        except (ConnectionRefusedError, OSError):
            raise
        except Exception:
            return
        stepping1 = status1.get("stepping") is True
        ticks1 = status1.get("ticks")

        # Decision matrix:
        # 1. stepping=True at either probe → CpuStateError (existing
        #    behavior: stepping field detected pause).
        if stepping0 or stepping1:
            contract = get_contract(event)
            raise CpuStateError(
                f"{event} requires CPU running (not stepping): "
                f"{contract.diagnostic_hint} "
                f"[stepping0={stepping0}, stepping1={stepping1}, "
                f"ticks0={ticks0}, ticks1={ticks1}, 50ms probe]"
            )
        # 2. stepping=False at both probes AND ticks present AND
        #    unchanged → CpuStateError (breakpoint pause suspected but
        #    stepping field didn't report — ticks backup validation).
        #    When ticks is None (not provided by transport, e.g.
        #    FakeTransport in tests or older PPSSPP builds), skip the
        #    ticks backup validation and rely on the stepping field only.
        if ticks0 is not None and ticks1 is not None and ticks0 == ticks1:
            raise CpuStateError(
                f"{event} requires CPU running: ticks unchanged "
                f"({ticks0} == {ticks1}) after 50ms, breakpoint pause "
                f"suspected but stepping field didn't report "
                f"[stepping0={stepping0}, stepping1={stepping1}]"
            )
        # 3. stepping=False at both probes AND ticks progressed →
        #    CPU advancing normally. Allow the ticketed call to proceed.

    async def gpu_stats(self, timeout: float = 5.0) -> dict[str, Any]:
        """Query GPU statistics (fps, vblanks, timing).

        Async ticketed: PPSSPP pushes stats on the next GPU flip, so the
        call returns when the next frame is rendered. Requires the game
        to be running (CPU not stepping); if CPU is paused, no frames
        are rendered and the call will timeout.

        Batch 1: pre-checks CPU state via `cpu.status` and raises
        CpuStateError immediately if stepping=True (instead of waiting
        `timeout` seconds for the silent timeout).

        Args:
            timeout: ticketed wait timeout in seconds (default 5.0).

        Returns:
            Dict with keys: `fps`, `vblanksPerSecond`, `info`, `timing`.

        Raises:
            CpuStateError: if CPU is currently stepping.
        """
        await self._require_running("gpu.stats.get")
        return await self._transport.call("gpu.stats.get", timeout=timeout)

    async def gpu_record_dump(self, timeout: float = 5.0) -> dict[str, Any]:
        """Capture a GPU record dump (GE command stream for one frame).

        Async ticketed: PPSSPP records the next frame's GE commands and
        returns them as a binary dump. Requires the game to be running
        (CPU not stepping); if CPU is paused, no frames are rendered
        and the call will timeout.

        Batch 1: pre-checks CPU state via `cpu.status` and raises
        CpuStateError immediately if stepping=True (instead of waiting
        `timeout` seconds for the silent timeout).

        Args:
            timeout: ticketed wait timeout in seconds (default 5.0).

        Returns:
            Dict with key `uri` containing a data: URI of form
            `data:application/octet-stream;base64,<base64-payload>` (see
            GPURecordSubscriber.cpp:L93). Strip the
            `data:application/octet-stream;base64,` prefix to recover the
            raw base64-encoded GE command dump. Other metadata fields
            (e.g. `ticket`) may be present.

        Raises:
            CpuStateError: if CPU is currently stepping.
        """
        await self._require_running("gpu.record.dump")
        return await self._transport.call("gpu.record.dump", timeout=timeout)

    # ======================================================================
    # Memory Info methods (C-M1) — 1 method
    # ======================================================================
    #
    # `memory.info.search` searches PPSSPP's memory tracking system for
    # allocation/write/texture metadata tags. Returns a single `extent`
    # (null when no match) describing the matched allocation: type /
    # address / size / ticks / pc / tag / allocated. All parameters
    # except `match` are optional. See MemoryInfoSubscriber.cpp:L52,
    # L324-393 for the response shape.

    async def search_memory_info(
        self,
        match: str,
        address: int | None = None,
        end: int | None = None,
        type: str | None = None,  # noqa: A002 — matches PPSSPP WS API
    ) -> dict[str, Any]:
        """Search memory allocation/write/texture metadata tags.

        Args:
            match: Case-insensitive substring to match against memory
                region tags (e.g., 'texture', 'vertex', 'framebuf').
            address: Optional start address for the search range.
            end: Optional end address for the search range.
            type: Optional type filter (e.g., 'texture', 'vertex').

        Returns:
            Dict with key `extent` containing either null (no match) or
            a single extent object describing the matched allocation
            (type, address, size, ticks, pc, tag, allocated). NOT a list
            — see MemoryInfoSubscriber.cpp:L52, L324-393.
        """
        params: dict[str, Any] = {"match": match}
        if address is not None:
            params["address"] = address
        if end is not None:
            params["end"] = end
        if type is not None:
            params["type"] = type
        return await self._transport.call("memory.info.search", **params)

    # ======================================================================
    # Memory Scan (C-M2) — 1 method (business orchestration)
    # ======================================================================
    #
    # D2 (ppsspp-dfx-tool-enhancement design): scan_memory is a business
    # orchestration layer over `read_bytes`, NOT a WS event wrapper.
    # PPSSPP WS debugger has no `memory.scan` event; scanning is done
    # client-side by chunked reads + Python `bytes.find()`. Default
    # chunk_size=4096 (4KB page-aligned). Patterns spanning chunk
    # boundaries are caught by overlapping reads by `len(pattern) - 1`
    # bytes. Unreadable regions are silently skipped.

    async def scan_memory(
        self,
        pattern: bytes,
        start: int,
        end: int,
        max_results: int = 100,
        chunk_size: int = 4096,
    ) -> list[dict[str, Any]]:
        """Client-side memory scan via chunked read_bytes + bytes.find.

        Reads memory in `chunk_size` chunks, searches each chunk for
        `pattern`, and collects matches with surrounding context.
        Patterns that span chunk boundaries are caught by overlapping
        reads by `len(pattern) - 1` bytes. Unreadable regions (e.g.,
        unmapped memory) are silently skipped.

        Args:
            pattern: Bytes pattern to search for (hex-decoded by caller).
            start: Start address (inclusive).
            end: End address (exclusive).
            max_results: Maximum number of matches to return (default 100).
            chunk_size: Bytes per read request (default 4096, 4KB aligned).

        Returns:
            List of match dicts: {address: int, context: str} where
            `address` is the match start address and `context` is the
            hex-encoded pattern bytes at that address (uppercase, no
            separators). Empty list if no matches or invalid range.
        """
        if not pattern:
            return []
        if end <= start:
            return []

        matches: list[dict[str, Any]] = []
        overlap = len(pattern) - 1
        # Each iteration issues ONE memory.read of
        # chunk + overlap bytes. The tool layer caps the pattern at 4096
        # bytes, but direct client callers can pass any length — clamp the
        # effective chunk so a single read stays within the documented
        # 64 KiB budget (the W4 clamp of chunk_size alone did not cover
        # the overlap tail).
        from ppsspp_dfx_mcp.core.primitives import MAX_SINGLE_READ_BYTES

        effective_chunk = max(1, min(chunk_size, MAX_SINGLE_READ_BYTES - overlap))
        cursor = start

        while cursor < end and len(matches) < max_results:
            # Read effective_chunk bytes; reserve overlap for boundary-
            # spanning patterns. The last chunk does not extend beyond `end`.
            chunk_end = min(cursor + effective_chunk, end)
            read_end = min(chunk_end + overlap, end) if chunk_end < end else chunk_end
            read_size = read_end - cursor
            if read_size <= 0:
                break

            try:
                data = await self.read_bytes(address=cursor, size=read_size)
            except Exception:
                # Skip unreadable regions (e.g., unmapped, permission
                # denied). The scan continues at the next chunk boundary.
                cursor = chunk_end
                continue

            # Search for all occurrences in this chunk, but only report
            # matches within [cursor, chunk_end) to avoid duplicates in
            # the overlap region (overlap bytes are re-read next iteration).
            search_start = 0
            while search_start < len(data):
                idx = data.find(pattern, search_start)
                if idx == -1:
                    break
                match_addr = cursor + idx
                if match_addr >= chunk_end:
                    break  # in overlap tail; next chunk will find it
                if match_addr < end:
                    context = data[idx : idx + len(pattern)].hex().upper()
                    matches.append(
                        {
                            "address": match_addr,
                            "context": context,
                        }
                    )
                    if len(matches) >= max_results:
                        break
                search_start = idx + 1

            # Advance to the next chunk boundary (overlap is re-read on
            # the next iteration to catch boundary-spanning patterns).
            # Use actual bytes read to avoid skipping memory on short reads.
            cursor = cursor + len(data) if len(data) < read_size else chunk_end

        return matches

    # ======================================================================
    # Replay methods — 8 methods
    # Thin proxies over the 7 PPSSPP replay.* WS events (see
    # ReplaySubscriber.cpp:26-37) + 1 client-side poller (wait_complete).
    # All events are NO_STEPPING — recording requires the CPU to be
    # RUNNING so captured button timings are real. See spike evidence
    # in docs/experiment/experiment_ppsspp_replay_spike_v1.md.
    # ======================================================================

    async def replay_begin(self) -> dict[str, Any]:
        """Begin or resume recording. No params, no extra response data."""
        return await self._transport.call("replay.begin")

    async def replay_abort(self) -> dict[str, Any]:
        """Abort any replay execution or recording; discards in-progress recording."""
        return await self._transport.call("replay.abort")

    async def replay_flush(self) -> dict[str, Any]:
        """Flush recorded data.

        Returns:
            dict with 'version' (int) and 'base64' (str). The binary size
            must be computed client-side (len of base64-decoded bytes).
            Fails with 'Game not running' if PSP not inited.
        """
        return await self._transport.call("replay.flush")

    async def replay_execute(self, version: int, base64: str) -> dict[str, Any]:
        """Execute a replay. Does NOT auto-end — poll replay.status afterwards.

        Args:
            version: Replay format version (from a prior replay.flush).
            base64: Base64-encoded replay data (from a prior replay.flush).
        """
        return await self._transport.call("replay.execute", version=version, base64=base64)

    async def replay_status(self) -> dict[str, Any]:
        """Get replay status.

        Returns:
            dict with 'executing' (bool) and 'saving' (bool).
        """
        return await self._transport.call("replay.status")

    async def replay_time_get(self) -> dict[str, Any]:
        """Get the base RTC (power-on time).

        Returns:
            dict with 'value' (int) — base RTC in seconds. Constant
            during a session. Fails if PSP not inited.
        """
        return await self._transport.call("replay.time.get")

    async def replay_time_set(self, value: int) -> dict[str, Any]:
        """Overwrite the base RTC.

        Args:
            value: Base RTC in seconds (uint32).
        """
        return await self._transport.call("replay.time.set", value=value)

    async def replay_wait_complete(
        self,
        timeout_ms: int = 10000,
        interval_ms: int = 100,
    ) -> dict[str, Any]:
        """Poll `replay.status` until `executing` becomes False or timeout.

        `replay.execute` does not auto-end a replay once fired; this
        — the executing flag stays True after the replay finishes, and
        only an explicit `replay.abort` or this poller can confirm the
        replay is done.

        Args:
            timeout_ms: Total timeout in milliseconds (default 10s).
            interval_ms: Polling interval in milliseconds (default 100ms).

        Returns:
            The final `replay.status` response dict (with executing=False
            if the wait succeeded, or executing=True if timed out).

        Raises:
            asyncio.TimeoutError: if `executing` does not become False
                within `timeout_ms`. The last status response is attached
                as `last_status` on the exception (use getattr).
        """
        start = asyncio.get_running_loop().time()
        timeout_s = timeout_ms / 1000.0
        interval_s = interval_ms / 1000.0
        last: dict[str, Any] = {}
        iterations = 0
        while True:
            iterations += 1
            last = await self._transport.call("replay.status")
            if not bool(last.get("executing", False)):
                return {**last, "_wait_iterations": iterations}
            elapsed = asyncio.get_running_loop().time() - start
            if elapsed >= timeout_s:
                raise TimeoutError(
                    f"replay.wait_complete timeout ({timeout_ms}ms) — "
                    f"executing still True after {iterations} polls"
                )
            await asyncio.sleep(interval_s)

    # ======================================================================
    # Safe query primitives (delegated to SteppingManager)
    # ======================================================================

    async def safe_get_pc(self) -> tuple[int, str]:
        """Safe get PC — pauses CPU, queries, returns (pc, TrustLevel.HIGH)."""
        return await self._stepping.safe_get_pc()

    async def safe_get_threads(self) -> ThreadSnapshot:
        """Safe get threads — pauses CPU, returns ThreadSnapshot."""
        return await self._stepping.safe_get_threads()

    # ======================================================================
    # Helpers
    # ======================================================================
    # _extract_pc moved to core/registers.py (shared with SteppingManager).
    # Use `extract_pc(regs)` directly.
