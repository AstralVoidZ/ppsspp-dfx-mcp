"""L3 orchestration tests: error translation (batch 5).

Anchors:
- D-25: asyncio.TimeoutError → WsTimeout with CPU state hint
- D-17: ConnectionRefusedError → WsDisconnected with session hint
- D-17: RuntimeError("WebSocket not connected") → WsDisconnected
- D-19: RuntimeError("PPSSPP error: Missing end parameter") → PpssppProtocolError
- D-19: RuntimeError("PPSSPP error: invalid address") → PpssppProtocolError with hint
- D-17: RuntimeError("CPU not stepping") → CpuStateError with pause hint
- ToolError subclasses preserved as-is (no double-wrapping)

L3 focus: to_tool_error pattern matching + diagnostic hint attachment.
"""

from __future__ import annotations

from ppsspp_dfx_mcp.errors import (
    CpuStateError,
    PortConflict,
    PpssppProtocolError,
    SessionNotFound,
    ToolError,
    WsDisconnected,
    WsTimeout,
    to_tool_error,
)

# ============================================================================
# D-25: asyncio.TimeoutError → WsTimeout
# ============================================================================


class TestTimeoutTranslation:
    """L3: asyncio.TimeoutError → WsTimeout with CPU state hint."""

    def test_asyncio_timeout_error_becomes_ws_timeout(self) -> None:
        """asyncio.TimeoutError → WsTimeout with code='WS_TIMEOUT'."""
        exc = TimeoutError()
        result = to_tool_error(exc)

        assert isinstance(result, WsTimeout)
        assert result.code == "WS_TIMEOUT"

    def test_builtin_timeout_error_becomes_ws_timeout(self) -> None:
        """Builtins TimeoutError → WsTimeout."""
        exc = TimeoutError("wait_for_state timeout (5000ms)")
        result = to_tool_error(exc)

        assert isinstance(result, WsTimeout)
        assert result.code == "WS_TIMEOUT"

    def test_timeout_hint_mentions_cpu_running(self) -> None:
        """WsTimeout message contains hint about REQUIRED_RUNNING."""
        exc = TimeoutError()
        result = to_tool_error(exc)

        msg = str(result)
        assert "REQUIRED_RUNNING" in msg, (
            "D-25: WsTimeout message must mention REQUIRED_RUNNING events."
        )

    def test_timeout_hint_mentions_resume(self) -> None:
        """WsTimeout hint suggests step(action='resume')."""
        exc = TimeoutError()
        result = to_tool_error(exc)

        assert "step(action='resume')" in str(result), (
            "D-25: WsTimeout hint must suggest step(action='resume') to "
            "resume the CPU before retrying."
        )


# ============================================================================
# D-17: ConnectionRefusedError → WsDisconnected
# ============================================================================


class TestConnectionRefusedTranslation:
    """L3: ConnectionRefusedError → WsDisconnected with session hint."""

    def test_connection_refused_becomes_ws_disconnected(self) -> None:
        """ConnectionRefusedError → WsDisconnected."""
        exc = ConnectionRefusedError("Connection refused")
        result = to_tool_error(exc)

        assert isinstance(result, WsDisconnected)
        assert result.code == "WS_DISCONNECTED"

    def test_ws_not_connected_becomes_ws_disconnected(self) -> None:
        """RuntimeError('WebSocket not connected') → WsDisconnected."""
        exc = RuntimeError("WebSocket not connected")
        result = to_tool_error(exc)

        assert isinstance(result, WsDisconnected)

    def test_ws_disconnected_hint_mentions_session(self) -> None:
        """WsDisconnected hint suggests ppsspp_session(action='start')."""
        exc = ConnectionRefusedError("Connection refused")
        result = to_tool_error(exc)

        msg = str(result)
        assert "ppsspp_session" in msg or "start a session" in msg.lower(), (
            "D-17: WsDisconnected hint must suggest starting a session."
        )


# ============================================================================
# D-19: PPSSPP error event → PpssppProtocolError
# ============================================================================


class TestPpssppProtocolErrorTranslation:
    """L3: RuntimeError('PPSSPP error: ...') → PpssppProtocolError."""

    def test_ppsspp_error_becomes_protocol_error(self) -> None:
        """RuntimeError('PPSSPP error: ...') → PpssppProtocolError."""
        exc = RuntimeError("PPSSPP error: something went wrong (level=2)")
        result = to_tool_error(exc)

        assert isinstance(result, PpssppProtocolError)
        assert result.code == "PPSSPP_PROTOCOL_ERROR"

    def test_missing_end_parameter_hint(self) -> None:
        """'Missing end parameter' error gets D-19 hint."""
        exc = RuntimeError("PPSSPP error: Missing end parameter (level=2)")
        result = to_tool_error(exc)

        msg = str(result)
        assert "count>0" in msg or "D-19" in msg, (
            "D-19: 'Missing end parameter' must get hint about count>0."
        )

    def test_invalid_address_hint(self) -> None:
        """'invalid address' error gets address range hint."""
        exc = RuntimeError("PPSSPP error: invalid address (level=2)")
        result = to_tool_error(exc)

        msg = str(result)
        assert "0x08800000" in msg or "convert_address" in msg, (
            "D-19: 'invalid address' must get hint about PSP memory range."
        )

    def test_unknown_ppsspp_error_no_hint(self) -> None:
        """Unknown PPSSPP error gets no hint but still PpssppProtocolError."""
        exc = RuntimeError("PPSSPP error: something unknown (level=1)")
        result = to_tool_error(exc)

        assert isinstance(result, PpssppProtocolError)
        # Message preserved without hint.
        assert "something unknown" in str(result)


# ============================================================================
# D-17: Stepping-related RuntimeError → CpuStateError
# ============================================================================


class TestCpuStateErrorTranslation:
    """L3: RuntimeError mentioning 'stepping' → CpuStateError."""

    def test_not_stepping_becomes_cpu_state_error(self) -> None:
        """RuntimeError('CPU not stepping') → CpuStateError."""
        exc = RuntimeError("CPU not stepping")
        result = to_tool_error(exc)

        assert isinstance(result, CpuStateError)
        assert result.code == "CPU_STATE_ERROR"

    def test_cpu_state_error_hint_mentions_pause(self) -> None:
        """CpuStateError hint suggests step(action='pause')."""
        exc = RuntimeError("CPU not stepping")
        result = to_tool_error(exc)

        msg = str(result)
        assert "step(action='pause')" in msg, (
            "D-17: CpuStateError hint must suggest step(action='pause')."
        )

    def test_not_paused_becomes_cpu_state_error(self) -> None:
        """RuntimeError('not paused') → CpuStateError."""
        exc = RuntimeError("CPU not paused")
        result = to_tool_error(exc)

        assert isinstance(result, CpuStateError)

    def test_not_paused_hint_mentions_pause(self) -> None:
        """CpuStateError for 'not paused' suggests step(action='pause')."""
        exc = RuntimeError("CPU not paused")
        result = to_tool_error(exc)

        msg = str(result)
        assert "step(action='pause')" in msg, (
            "D-17: CpuStateError for 'not paused' must suggest "
            "step(action='pause') — CPU is running, needs to pause."
        )


# ============================================================================
# Preservation: ToolError subclasses preserved as-is
# ============================================================================


class TestToolErrorPreservation:
    """L3: ToolError subclasses are preserved, not double-wrapped."""

    def test_session_not_found_preserved(self) -> None:
        """SessionNotFound is preserved with its code."""
        exc = SessionNotFound("session abc not found")
        result = to_tool_error(exc)

        assert result is exc
        assert result.code == "SESSION_NOT_FOUND"

    def test_port_conflict_preserved(self) -> None:
        """PortConflict is preserved with its code."""
        exc = PortConflict("port 12345 in use")
        result = to_tool_error(exc)

        assert result is exc
        assert result.code == "PORT_CONFLICT"

    def test_cpu_state_error_preserved(self) -> None:
        """CpuStateError raised directly is preserved (not re-wrapped)."""
        exc = CpuStateError("CPU stepping for gpu_stats")
        result = to_tool_error(exc)

        assert result is exc
        assert result.code == "CPU_STATE_ERROR"

    def test_generic_toolerror_preserved(self) -> None:
        """Plain ToolError is preserved with INTERNAL code."""
        exc = ToolError("some error")
        result = to_tool_error(exc)

        assert result is exc
        assert result.code == "INTERNAL"


# ============================================================================
# Default: unknown exception → INTERNAL
# ============================================================================


class TestDefaultInternal:
    """L3: Unknown exceptions default to ToolError with INTERNAL code."""

    def test_value_error_becomes_internal(self) -> None:
        """ValueError → ToolError with code='INTERNAL'."""
        exc = ValueError("bad value")
        result = to_tool_error(exc)

        assert isinstance(result, ToolError)
        assert result.code == "INTERNAL"
        assert "bad value" in str(result)

    def test_runtime_error_no_pattern_becomes_internal(self) -> None:
        """RuntimeError with no matching pattern → INTERNAL."""
        exc = RuntimeError("something completely different")
        result = to_tool_error(exc)

        assert result.code == "INTERNAL"
