"""Session view — public JSON contract for ppsspp_session / ppsspp_session_list."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.models.session import Session, WaitReadyResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class SessionResponse(FrozenModel):
    """Response view for a single session (start/stop/get)."""

    session_id: str = Field(description="Session UUID-like identifier.")
    iso_path: str = Field(description="Absolute path to the ISO file.")
    pid: int | None = Field(
        default=None,
        description="PPSSPP process PID (None if stopped).",
    )
    # ws_url is exposed intentionally: agents may need to know which
    # host:port the debugger is listening on (e.g. to attach an external
    # tool) without re-reading project config.
    ws_url: str = Field(
        description="WebSocket URL (ws://host:port/debugger)."
    )
    created_at: str = Field(description="ISO 8601 timestamp of session creation.")
    last_active_at: str = Field(
        description="ISO 8601 timestamp of last tool call."
    )
    exec_count: int = Field(
        default=0,
        description="Number of tool calls made against this session.",
    )
    ws_connected: bool = Field(
        default=False,
        description="True if WebSocket is currently connected.",
    )
    recovered: int = Field(
        default=0,
        description=(
            "H2: resilient-start relaunch count (0 = the first launch "
            "succeeded; >0 means the game state was reset by a wedge "
            "heal — breakpoints need re-arming)."
        ),
    )
    restored: int = Field(
        default=0,
        description=(
            "F-6(a): 1 when this session was restored from sessions.json "
            "(a previous server run left it behind) rather than started "
            "fresh in this process — its game state may be stale."
        ),
    )
    ppsspp_version: dict[str, Any] | None = Field(
        default=None,
        description=(
            "PPSSPP build fingerprint captured from the version "
            "handshake (None until the session transport binds)."
        ),
    )

    @classmethod
    def from_session(cls, sess: Session) -> "SessionResponse":
        """Construct from a Session domain model.

        Datetime fields (created_at, last_active_at) are serialized to ISO
        8601 strings for the JSON contract; the in-memory Session model
        stores them as `datetime` objects.
        """
        return cls(
            session_id=sess.session_id,
            iso_path=sess.iso_path,
            pid=sess.pid,
            ws_url=sess.ws_url,
            created_at=sess.created_at.isoformat(),
            last_active_at=sess.last_active_at.isoformat(),
            exec_count=sess.exec_count,
            ws_connected=sess.ws_connected,
            recovered=int(sess.extra.get("recovered", 0)),
            restored=int(bool(sess.extra.get("restored"))),
            ppsspp_version=sess.extra.get("ppsspp_version"),
        )

    @classmethod
    def from_domain(cls, data: Any) -> "SessionResponse":
        """Override to accept Session dataclass or dict."""
        if isinstance(data, Session):
            return cls.from_session(data)
        if isinstance(data, dict):
            return cls(**data)
        raise TypeError(f"from_domain() cannot convert {type(data).__name__}")


class SessionListResponse(FrozenModel):
    """Response view for ppsspp_session_list."""

    sessions: list[SessionResponse] = Field(
        default_factory=list,
        description="Active sessions.",
    )
    count: int = Field(default=0, description="Number of sessions.")

    @classmethod
    def from_sessions(cls, sessions: list[Session]) -> "SessionListResponse":
        return cls(
            sessions=[SessionResponse.from_session(s) for s in sessions],
            count=len(sessions),
        )


class WaitReadyResponse(FrozenModel):
    """Response view for ppsspp_session(action='wait_ready') (H0, 2026-09-07)."""

    action: str = Field(
        default="wait_ready",
        description="Literal 'wait_ready' (echoes the session action).",
    )
    ready: bool = Field(
        description="True when the CPU-start probe succeeded.",
    )
    elapsed_s: float = Field(
        description="Wall-clock seconds spent polling.",
    )
    probe_addr: str = Field(
        description="Polled address, hex string (default top.prx base).",
    )
    probe_value: str | None = Field(
        default=None,
        description="u32 read at probe_addr once ready, hex string "
        "(None in fake mode).",
    )
    note: str | None = Field(
        default=None,
        description="Optional human context (e.g. fake-mode short-circuit).",
    )

    @classmethod
    def from_result(cls, result: WaitReadyResult) -> "WaitReadyResponse":
        probe_value: str | None = None
        if result.probe_value is not None:
            probe_value = f"0x{result.probe_value:08X}"
        return cls(
            action="wait_ready",
            ready=result.ready,
            elapsed_s=result.elapsed_s,
            probe_addr=format_address(result.probe_addr),
            probe_value=probe_value,
            note=result.note,
        )
