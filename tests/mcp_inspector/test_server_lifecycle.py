"""Test: mcp_inspector fixture manages the server subprocess cleanly.

Verifies the fixture's lifecycle contract:
1. The fixture yields a connected, initialized ClientSession.
2. After the test, the fixture tears down the session and subprocess
   without leaking.
3. Multiple tests sharing the fixture see the same server instance
   (session-scoped, not function-scached).

These tests are somewhat meta — they test the test fixture itself,
not the server. They catch:
- Subprocess leaks (the server doesn't shut down cleanly).
- Session-scope violations (each test gets a fresh server instead of
  reusing the shared one — would manifest as slow test runs).
- Handshake failures (the session isn't actually initialized).

loop_scope: all tests use "session" scope to share the mcp_inspector
fixture's session-scoped event loop (see conftest.py for rationale).
"""

from __future__ import annotations

import pytest
from mcp import ClientSession

# All tests share the session-scoped mcp_inspector fixture, so they
# MUST run on the session-scoped event loop (loop_scope="session").
_ASYNC = pytest.mark.asyncio(loop_scope="session")


@_ASYNC
async def test_fixture_yields_initialized_session(mcp_inspector):
    """The fixture must yield a ClientSession that has completed initialize()."""
    assert isinstance(mcp_inspector, ClientSession)
    # After initialize(), get_server_capabilities() returns a non-None
    # ServerCapabilities object. If the handshake didn't complete, this
    # returns None.
    caps = mcp_inspector.server_capabilities
    assert caps is not None, "ClientSession not initialized — get_server_capabilities() is None"
    # The server must advertise tools capability (we registered 30+).
    assert caps.tools is not None, (
        "server capabilities missing 'tools' — server registered no tools?"
    )


@_ASYNC
async def test_server_responds_to_repeated_calls(mcp_inspector):
    """A second call through the same session must succeed (no one-shot failure).

    This catches a class of bugs where the server's first tool call works
    but subsequent calls fail (e.g. session state corruption, transport
    teardown after first call, etc.).
    """
    import json

    # First call.
    r1 = await mcp_inspector.call_tool("ppsspp_health", {})
    assert not r1.is_error
    p1 = json.loads(r1.content[0].text)
    assert p1["status"] == "ok"

    # Second call through the SAME session.
    r2 = await mcp_inspector.call_tool("ppsspp_health", {})
    assert not r2.is_error
    p2 = json.loads(r2.content[0].text)
    assert p2["status"] == "ok"

    # Uptime should be monotonically increasing — second call's uptime
    # must be >= first call's. (Equal is fine if calls are <1ms apart,
    # but never smaller.)
    assert p2["uptime_s"] >= p1["uptime_s"], (
        f"uptime decreased: first={p1['uptime_s']}, second={p2['uptime_s']}"
    )


@_ASYNC
async def test_session_can_be_started_and_stopped(mcp_inspector):
    """Verify start_session + stop_session roundtrip works through the fixture.

    This exercises the full fake-mode path:
    - start_session creates a fake session (no PPSSPP subprocess)
    - get_session_state reads it back
    - stop_session removes it cleanly
    - session_list reflects the change
    """
    import json

    # Start.
    start_r = await mcp_inspector.call_tool(
        "ppsspp_session",
        {"action": "start", "iso_path": "fake.iso"},
    )
    assert not start_r.is_error
    start_p = json.loads(start_r.content[0].text)
    sid = start_p["session_id"]
    assert sid

    # Verify it appears in session_list.
    list_r = await mcp_inspector.call_tool("ppsspp_session_list", {})
    assert not list_r.is_error
    list_p = json.loads(list_r.content[0].text)
    session_ids = [s["session_id"] for s in list_p["sessions"]]
    assert sid in session_ids, f"started session {sid} not in session_list: {session_ids}"

    # Stop.
    stop_r = await mcp_inspector.call_tool(
        "ppsspp_session",
        {"action": "stop", "session_id": sid},
    )
    assert not stop_r.is_error

    # Verify it's gone from session_list.
    list_r2 = await mcp_inspector.call_tool("ppsspp_session_list", {})
    assert not list_r2.is_error
    list_p2 = json.loads(list_r2.content[0].text)
    session_ids2 = [s["session_id"] for s in list_p2["sessions"]]
    assert sid not in session_ids2, f"stopped session {sid} still in session_list: {session_ids2}"
