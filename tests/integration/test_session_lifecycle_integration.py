"""test_session_lifecycle_integration.py — integration: session tool end-to-end.

Anchor: tools/session.py `session()` + `session_list()` → session_manager
→ Session domain model → SessionResponse/SessionListResponse views.

Integration scope:
1. Error paths (no real PPSSPP required) — verify the wiring from the
   MCP-facing tool function down to the session manager for invalid inputs.
2. Real-PPSSPP end-to-end (Phase 6) — verify the production happy path:
   start_session launches a real PPSSPP process, session_list reflects
   the new session, stop_session terminates it cleanly.

Contract (error paths):
- `session(action='start', iso_path=<missing>)` → ToolError(ISO_NOT_FOUND)
- `session(action='start')` (no iso_path) → ToolError(INTERNAL)
- `session(action='stop', session_id=<missing>)` → ToolError(SESSION_NOT_FOUND)
- `session(action='get', session_id=<missing>)` → ToolError(SESSION_NOT_FOUND)
- `session(action=<invalid>)` → ToolError(INTERNAL)
- `session_list()` → dict with `sessions` (list) + `count` (int) fields

Contract (real-PPSSPP path, skipped when PPSSPP unavailable):
- `session(action='start', iso_path=<real>)` returns session_id, pid>0,
  ws_url of form ``ws://127.0.0.1:<port>/debugger``.
- `session_list()` after start contains the new session_id.
- `session(action='stop', session_id=<new>)` returns the stopped session
  with pid=None; subsequent `session_list()` does NOT contain it.
- `session(action='get', session_id=<new>)` returns the active session.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from ppsspp_dfx_mcp.errors import (
    IsoNotFound,
    SessionNotFound,
    ToolError,
)
from ppsspp_dfx_mcp.session import session_manager as sm_mod
from ppsspp_dfx_mcp.tools.session import session, session_list


# ============================================================================
# Fixtures: isolate sessions.json + stub launcher for each test
# ============================================================================


@pytest.fixture
def sessions_file(tmp_path: Path) -> Path:
    """Path for sessions.json isolated to each test."""
    return tmp_path / "sessions.json"


@pytest.fixture(autouse=True)
def patch_sessions_path(sessions_file: Path):
    """Patch sessions_path() to return the test's tmp sessions.json.

    Also resets the module-level singleton so each test builds a fresh
    SessionManager (the previous one may have cached state).
    """
    with patch.object(sm_mod, "sessions_path", return_value=sessions_file):
        sm_mod._default_manager = None
        yield
        sm_mod._default_manager = None


# ============================================================================
# session(action='start') error paths
# ============================================================================


class TestSessionStartErrorPaths:
    """session(action='start') error paths."""

    async def test_start_missing_iso_raises_iso_not_found(self, tmp_path):
        """action='start' with a non-existent ISO path raises IsoNotFound."""
        missing_iso = tmp_path / "does_not_exist.iso"
        with pytest.raises(IsoNotFound) as exc_info:
            await session(action="start", iso_path=str(missing_iso))
        assert "ISO file not found" in str(exc_info.value)
        assert exc_info.value.code == "ISO_NOT_FOUND"

    async def test_start_without_iso_path_raises_tool_error(self):
        """action='start' without iso_path raises ToolError(INTERNAL)."""
        with pytest.raises(ToolError) as exc_info:
            await session(action="start", iso_path=None)
        assert "iso_path is required" in str(exc_info.value)
        assert exc_info.value.code == "INTERNAL"

    async def test_start_empty_iso_path_raises_tool_error(self):
        """action='start' with empty string iso_path raises ToolError(INTERNAL)."""
        with pytest.raises(ToolError) as exc_info:
            await session(action="start", iso_path="")
        assert "iso_path is required" in str(exc_info.value)
        assert exc_info.value.code == "INTERNAL"


# ============================================================================
# session(action='stop') / session(action='get') error paths
# ============================================================================


class TestSessionStopGetErrorPaths:
    """session(action='stop') / session(action='get') with unknown sid."""

    async def test_stop_unknown_session_raises_session_not_found(self):
        """action='stop' with an unknown session_id raises SessionNotFound."""
        with pytest.raises(SessionNotFound) as exc_info:
            await session(action="stop", session_id="nonexistent-sid")
        assert "session not found" in str(exc_info.value)
        assert exc_info.value.code == "SESSION_NOT_FOUND"

    async def test_get_unknown_session_raises_session_not_found(self):
        """action='get' with an unknown session_id raises SessionNotFound."""
        with pytest.raises(SessionNotFound) as exc_info:
            await session(action="get", session_id="nonexistent-sid")
        assert "session not found" in str(exc_info.value)
        assert exc_info.value.code == "SESSION_NOT_FOUND"

    async def test_stop_without_session_id_raises_tool_error(self):
        """action='stop' without session_id raises ToolError(INTERNAL)."""
        with pytest.raises(ToolError) as exc_info:
            await session(action="stop", session_id=None)
        assert "session_id is required" in str(exc_info.value)
        assert exc_info.value.code == "INTERNAL"

    async def test_get_without_session_id_raises_tool_error(self):
        """action='get' without session_id raises ToolError(INTERNAL)."""
        with pytest.raises(ToolError) as exc_info:
            await session(action="get", session_id=None)
        assert "session_id is required" in str(exc_info.value)
        assert exc_info.value.code == "INTERNAL"


# ============================================================================
# session(action=<invalid>) error path
# ============================================================================


class TestSessionInvalidAction:
    """session(action=<invalid>) raises ToolError(INTERNAL)."""

    async def test_invalid_action_raises_tool_error(self):
        """An invalid action value raises ToolError with code=INTERNAL."""
        with pytest.raises(ToolError) as exc_info:
            await session(action="invalid_action")
        assert "invalid action" in str(exc_info.value)
        assert exc_info.value.code == "INTERNAL"


# ============================================================================
# session_list() shape
# ============================================================================


class TestSessionListShape:
    """session_list() returns a dict with `sessions` + `count` fields."""

    async def test_session_list_returns_dict_with_sessions_and_count(self):
        """session_list() dict must have `sessions` (list) + `count` (int)."""
        result = await session_list()
        assert isinstance(result, dict)
        assert "sessions" in result
        assert "count" in result
        assert isinstance(result["sessions"], list)
        assert isinstance(result["count"], int)
        assert result["count"] == len(result["sessions"])

    async def test_session_list_empty_when_no_sessions(self):
        """session_list() returns count=0 when no sessions are active."""
        result = await session_list()
        assert result["count"] == 0
        assert result["sessions"] == []


# ============================================================================
# Phase 6: real-PPSSPP end-to-end (skipped when PPSSPP unavailable)
# ============================================================================


_WS_URL_PATTERN = re.compile(r"^ws://127\.0\.0\.1:\d+/debugger$")


@pytest.mark.real_ppsspp
class TestRealSessionLifecycle:
    """Real-PPSSPP end-to-end lifecycle: start → list → get → stop.

    These tests exercise the production code path verbatim:
    session_manager.start_session → launcher.start → PPSSPP subprocess →
    WsTransport connect → tool calls → stop_session → launcher.stop.

    Each test uses the `real_session` fixture (function-scoped), which
    launches its own PPSSPP subprocess via session_manager. Tests are
    skipped when PPSSPP or the ISO is unavailable (CI-safe).
    """

    async def test_start_returns_session_with_pid_and_ws_url(
        self, real_session, iso_path,
    ):
        """session(action=start) returns session_id, pid>0, valid ws_url."""
        # real_session fixture already called start_session; verify its
        # return value by querying the active session via session(action=get).
        result = await session(action="get", session_id=real_session)
        assert result["session_id"] == real_session
        assert result["iso_path"] == str(iso_path)
        assert result["pid"] is not None and result["pid"] > 0, (
            f"pid should be >0 for real session, got {result.get('pid')}"
        )
        assert _WS_URL_PATTERN.match(result["ws_url"]), (
            f"ws_url should match ws://127.0.0.1:<port>/debugger, "
            f"got {result['ws_url']!r}"
        )

    async def test_session_list_contains_active_session(self, real_session):
        """session_list() must include the session_id returned by start."""
        result = await session_list()
        session_ids = [s["session_id"] for s in result["sessions"]]
        assert real_session in session_ids, (
            f"active session {real_session} not in session_list: {session_ids}"
        )
        # The listed entry must report the same pid (process alive).
        matching = [s for s in result["sessions"] if s["session_id"] == real_session]
        assert matching, "session_id not found in session_list"
        assert matching[0]["pid"] is not None and matching[0]["pid"] > 0

    async def test_get_returns_active_session_with_pid(self, real_session):
        """session(action=get) returns the active session with pid>0."""
        result = await session(action="get", session_id=real_session)
        assert result["session_id"] == real_session
        assert result["pid"] is not None and result["pid"] > 0

    async def test_stop_removes_session_from_list(self, real_session):
        """After stop, session_list no longer contains the session_id.

        Note: the `real_session` fixture calls stop_session on teardown,
        but this test triggers stop_session explicitly and verifies the
        session is gone BEFORE the fixture's teardown runs. The fixture's
        teardown will then be a no-op (stop_session on already-stopped
        session raises SessionNotFound, which is swallowed by the
        fixture's best-effort cleanup).
        """
        # Stop the session explicitly.
        stopped = await session(action="stop", session_id=real_session)
        assert stopped["session_id"] == real_session
        # Stopped session reports pid=None (process terminated).
        assert stopped["pid"] is None, (
            f"stopped session should have pid=None, got {stopped.get('pid')}"
        )
        # session_list should no longer contain it.
        result = await session_list()
        session_ids = [s["session_id"] for s in result["sessions"]]
        assert real_session not in session_ids, (
            f"stopped session {real_session} still in session_list: {session_ids}"
        )

    async def test_get_after_stop_raises_session_not_found(self, real_session):
        """session(action=get) on a stopped session raises SessionNotFound."""
        await session(action="stop", session_id=real_session)
        with pytest.raises(SessionNotFound) as exc_info:
            await session(action="get", session_id=real_session)
        assert exc_info.value.code == "SESSION_NOT_FOUND"

    async def test_session_list_count_increments_with_sessions(
        self, real_session,
    ):
        """session_list() count is >=1 when at least one session is active."""
        result = await session_list()
        assert result["count"] >= 1, (
            f"count should be >=1 with an active session, got {result['count']}"
        )
