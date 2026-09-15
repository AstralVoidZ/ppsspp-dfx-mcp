"""Test: tool calls work end-to-end via the MCP protocol layer.

Verifies that ppsspp_health returns status="ok" and the expected
metadata fields when called via ClientSession.call_tool (the full
JSON-RPC round-trip: client → server → tool → transport → response).

The fake-mode server substitutes a FakeTransport for PPSSPP, but
ppsspp_health is a pure server-liveness probe that doesn't touch the
transport — so its response shape is identical in fake and real modes.

ppsspp_session(action=start) DOES exercise the fake-mode transport
substitution path (start_session → fake session → session_client →
FakeTransport). We assert it returns a session_id, then use that id
for a follow-up read_memory call to verify the full stack works.

loop_scope: all tests use "session" scope to share the mcp_inspector
fixture's session-scoped event loop (see conftest.py for rationale).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

# All tests share the session-scoped mcp_inspector fixture, so they
# MUST run on the session-scoped event loop (loop_scope="session").
_ASYNC = pytest.mark.asyncio(loop_scope="session")


@_ASYNC
async def test_ppsspp_health_returns_ok(mcp_inspector):
    """ppsspp_health must return status='ok' with metadata fields.

    The health tool is a pure server-liveness probe — it reads
    in-memory counters (uptime, tool_count, session_count) and never
    touches the transport. So its response shape is identical in fake
    and real modes.
    """
    result = await mcp_inspector.call_tool("ppsspp_health", {})

    assert not result.is_error, (
        f"ppsspp_health returned error: {result.content!r}"
    )
    # Result content is a list of TextContent. Parse the JSON payload.
    assert len(result.content) >= 1
    text_content = result.content[0]
    payload: dict[str, Any] = json.loads(text_content.text)

    assert payload["status"] == "ok", (
        f"expected status='ok', got {payload.get('status')!r}"
    )
    # Sanity: tool_count should be >= 30 (Phase 1-5 static tools).
    assert payload["tool_count"] >= 30, (
        f"tool_count should be >=30, got {payload['tool_count']}"
    )
    assert "uptime_s" in payload
    assert "python_version" in payload
    assert "pydantic_version" in payload


@_ASYNC
async def test_ppsspp_health_idempotent(mcp_inspector):
    """Calling ppsspp_health twice must return ok both times (no state mutation)."""
    for _ in range(2):
        result = await mcp_inspector.call_tool("ppsspp_health", {})
        assert not result.is_error
        payload = json.loads(result.content[0].text)
        assert payload["status"] == "ok"


@_ASYNC
async def test_session_start_then_read_memory_roundtrip(mcp_inspector):
    """Full fake-mode roundtrip: start_session → read_memory.

    This verifies the entire fake-mode transport substitution path:
    1. ppsspp_session(action=start, iso_path=...) creates a fake session
       (no PPSSPP subprocess spawned in fake mode).
    2. ppsspp_read_memory(action=read_u32, address=...) routes through
       session_client_with_transport → FakeTransport + load_all →
       returns the recorded fixture response.
    """
    # 1. Start a fake session (iso_path is irrelevant in fake mode but
    #    the API requires a non-empty string).
    start_result = await mcp_inspector.call_tool(
        "ppsspp_session",
        {"action": "start", "iso_path": "fake.iso"},
    )
    assert not start_result.is_error, (
        f"ppsspp_session(start) failed: {start_result.content!r}"
    )
    start_payload = json.loads(start_result.content[0].text)
    session_id = start_payload["session_id"]
    assert session_id, "start_session returned empty session_id"

    # 2. read_u32 — should return the recorded fixture's value field.
    #    The fixture at fixtures/memory.read_u32.json was recorded with
    #    address=0x08804000 and a real PPSSPP value. load_all injects
    #    the first record's response, so any address param will return
    #    that value (FakeTransport.call ignores params for fixed responses).
    #    address 传 hex string — 所有工具的地址参数统一为 str 类型
    #    (parse_address 解析 '0x08804000' 格式)，避免 Agent 做进制转换。
    read_result = await mcp_inspector.call_tool(
        "ppsspp_read_memory",
        {
            "session_id": session_id,
            "action": "read_u32",
            "address": "0x08804000",
        },
    )
    assert not read_result.is_error, (
        f"ppsspp_read_memory failed: {read_result.content!r}"
    )
    read_payload = json.loads(read_result.content[0].text)
    # value field must be present (recorded fixture has it).
    assert "value" in read_payload, (
        f"read_memory response missing 'value' field: {read_payload!r}"
    )
