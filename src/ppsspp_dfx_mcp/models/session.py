"""Session domain model (frozen dataclass).

This is the in-process representation of a PPSSPP debug session.
Persisted to ~/.ppsspp-dfx/sessions.json as JSON — `created_at` /
`last_active_at` are `datetime` objects in memory, serialized to ISO 8601
strings on disk (see session.session_manager._session_to_dict).

The launcher is NOT stored on the Session model — it is a runtime-only
object tracked in `SessionManager._launchers` (in-memory dict keyed by
session_id). Sessions loaded from disk never have a launcher (the process
is already gone). This avoids a dual source-of-truth between the model
field and the manager's dict.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any


def _now_utc() -> datetime:
    """Timezone-aware UTC now (single source for default_factory)."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class Session:
    """A PPSSPP debug session.

    Attributes:
        session_id: UUID-like string identifier.
        iso_path: Absolute path to the ISO file.
        pid: PPSSPP process PID (None if not started).
        ws_url: WebSocket URL (ws://host:port/debugger).
        created_at: Timezone-aware datetime of session creation.
        last_active_at: Timezone-aware datetime of last tool call.
        exec_count: Number of tool calls made against this session.
        ws_connected: True if WebSocket is currently connected.
        extra: Free-form metadata (e.g. game title, region).
    """

    session_id: str
    iso_path: str
    pid: int | None = None
    ws_url: str = "ws://127.0.0.1:12345/debugger"
    created_at: datetime = field(default_factory=_now_utc)
    last_active_at: datetime = field(default_factory=_now_utc)
    exec_count: int = 0
    ws_connected: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def with_updated_activity(self) -> Session:
        """Return a new Session with last_active_at = now and exec_count + 1."""
        # Pass extra explicitly as a shallow copy — without this, two
        # Session objects would share the same mutable dict reference
        # (dataclasses.replace preserves the original field value for
        # unspecified fields, NOT a copy). If Session is ever unfrozen
        # or extra is mutated via object.__setattr__, changes to one
        # would silently corrupt the other.
        return replace(
            self,
            last_active_at=_now_utc(),
            exec_count=self.exec_count + 1,
            extra=self.extra.copy(),
        )

    def with_stopped(self) -> Session:
        """Return a new Session marked as stopped (pid=None, ws_connected=False)."""
        return replace(
            self,
            pid=None,
            ws_connected=False,
            extra=self.extra.copy(),
        )

    def with_ws_connected(self, connected: bool) -> Session:
        """Return a new Session with ws_connected updated.

        Called by session_manager.update_ws_connected after a successful
        WS connect+handshake (connected=True) or on disconnect (False).
        The previous code never updated ws_connected after session creation
        (it stayed False forever), making it a dead field in the
        session(action="list") output.
        """
        return replace(
            self,
            ws_connected=connected,
            extra=self.extra.copy(),
        )


@dataclass(frozen=True)
class WaitReadyResult:
    """Result of ``ppsspp_session(action='wait_ready')`` (H0, 2026-09-07).

    Attributes:
        ready: True when the CPU-start probe succeeded.
        elapsed_s: Wall-clock seconds spent polling.
        probe_addr: Address polled (default top.prx base 0x08804000).
        probe_value: The u32 read at probe_addr once ready (None in
            fake mode, where there is no boot concept).
        note: Optional human context (e.g. fake-mode short-circuit).
    """

    ready: bool = True
    elapsed_s: float = 0.0
    probe_addr: int = 0x08804000
    probe_value: int | None = None
    note: str | None = None
