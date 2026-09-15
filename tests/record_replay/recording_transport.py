"""RecordingTransport — decorator over WsTransport that records all WS
communication to a cassette file (JSONL).

Composition over inheritance: holds a `real_transport: WsTransport` and
implements the same implicit protocol (call / fire_and_forget /
wait_for_state / wait_for_broadcast). All communication is forwarded to
the real transport AND appended to an in-memory record buffer. `flush()`
writes the buffer to a JSONL cassette file.

State-change capture: when `fire_and_forget` is invoked, the transport
reads `cpu.status` before and after the call to capture any state delta
caused by the side-effectful event (e.g. `cpu.stepping` flips `stepping`
to True). The delta is recorded as a `state_change` record so the
ReplayTransport can MERGE it into its own state during replay.

Design decisions (see design.md):
- Decision 1: decorator pattern (composition), no WsTransport inheritance
- Decision 2: JSONL cassette format (streaming writes, line-level parse)
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .cassette import CassetteRecord, save_cassette

logger = logging.getLogger(__name__)


class RecordingTransport:
    """Decorator over WsTransport that records all communication.

    Implements the same implicit protocol as WsTransport (call /
    fire_and_forget / wait_for_state / wait_for_broadcast). Each call
    is forwarded to the real transport AND recorded to an in-memory
    buffer. `flush()` writes the buffer to a JSONL cassette file.
    """

    def __init__(
        self,
        real_transport: Any,
        cassette_path: Optional[Path] = None,
        capture_state_changes: bool = True,
    ) -> None:
        """Initialize the recording decorator.

        Args:
            real_transport: the WsTransport instance to wrap. Must
                implement call / fire_and_forget / wait_for_state /
                wait_for_broadcast / events (the implicit protocol).
            cassette_path: optional path to write the cassette on
                flush(). If None, records stay in-memory only.
            capture_state_changes: if True, fire_and_forget reads
                cpu.status before and after the call to capture state
                deltas (recorded as state_change records).
        """
        self._real = real_transport
        self._cassette_path = cassette_path
        self._capture_state_changes = capture_state_changes
        self._records: list[CassetteRecord] = []
        # Mirror WsTransport.events so consumers can subscribe to
        # broadcasts transparently (the decorator does not intercept
        # broadcast queueing — broadcasts are recorded as they flow
        # through wait_for_broadcast).
        self._events_queue = real_transport.events

    # ---------- WsTransport protocol passthrough ----------

    @property
    def events(self) -> Any:
        """Passthrough to the real transport's events queue."""
        return self._events_queue

    @property
    def host(self) -> Any:
        return getattr(self._real, "host", None)

    @property
    def port(self) -> Any:
        return getattr(self._real, "port", None)

    @property
    def records(self) -> list[CassetteRecord]:
        """Read-only access to the in-memory record buffer."""
        return list(self._records)

    async def call(
        self, event: str, timeout: float = 5.0, **params: Any
    ) -> dict[str, Any]:
        """Forward call to real transport and record the exchange."""
        response = await self._real.call(event, timeout=timeout, **params)
        self._records.append(
            CassetteRecord(
                type="call",
                event=event,
                params=dict(params),
                response=response,
                timestamp=time.time(),
            )
        )
        return response

    async def fire_and_forget(self, event: str, **params: Any) -> None:
        """Forward fire-and-forget to real transport and record it.

        If capture_state_changes is True, reads cpu.status before and
        after the call to capture any state delta caused by the event.
        The delta is recorded as a state_change record so ReplayTransport
        can MERGE it during replay.
        """
        before_state: Optional[dict[str, Any]] = None
        if self._capture_state_changes:
            try:
                before_state = await self._real.call("cpu.status", timeout=2.0)
            except Exception as exc:
                logger.debug(
                    "fire_and_forget('%s'): pre-call cpu.status failed: %s",
                    event,
                    exc,
                )
                before_state = None

        await self._real.fire_and_forget(event, **params)
        self._records.append(
            CassetteRecord(
                type="fire_and_forget",
                event=event,
                params=dict(params),
                timestamp=time.time(),
            )
        )

        if self._capture_state_changes and before_state is not None:
            try:
                after_state = await self._real.call("cpu.status", timeout=2.0)
            except Exception as exc:
                logger.debug(
                    "fire_and_forget('%s'): post-call cpu.status failed: %s",
                    event,
                    exc,
                )
                return
            delta = {
                k: after_state[k]
                for k in after_state
                if k not in before_state or before_state[k] != after_state[k]
            }
            if delta:
                self._records.append(
                    CassetteRecord(
                        type="state_change",
                        event=event,
                        state_delta=delta,
                        timestamp=time.time(),
                    )
                )

    async def wait_for_state(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout_ms: int = 3000,
        interval_ms: int = 50,
    ) -> dict[str, Any]:
        """Forward to real transport's wait_for_state."""
        return await self._real.wait_for_state(
            predicate, timeout_ms=timeout_ms, interval_ms=interval_ms
        )

    async def wait_for_broadcast(
        self,
        event: str,
        timeout_ms: int = 5000,
        filter: Optional[Callable[[dict[str, Any]], bool]] = None,
    ) -> dict[str, Any]:
        """Forward to real transport's wait_for_broadcast and record the
        received broadcast message."""
        msg = await self._real.wait_for_broadcast(
            event, timeout_ms=timeout_ms, filter=filter
        )
        self._records.append(
            CassetteRecord(
                type="broadcast",
                event=event,
                message=msg,
                timestamp=time.time(),
            )
        )
        return msg

    # ---------- Recording lifecycle ----------

    def flush(self) -> None:
        """Write the in-memory record buffer to the cassette file.

        If cassette_path is None, this is a no-op (records stay in-memory
        for inspection via `.records`).
        """
        if self._cassette_path is None:
            return
        save_cassette(self._records, self._cassette_path)
        logger.info(
            "cassette flushed: %s (%d records)",
            self._cassette_path,
            len(self._records),
        )

    def clear(self) -> None:
        """Clear the in-memory record buffer."""
        self._records.clear()
