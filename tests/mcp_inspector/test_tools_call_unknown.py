"""Test: calling an unknown tool returns an MCP error (not silent success).

Verifies the server's tool-dispatch error handling at the MCP protocol
layer. Calling a non-existent tool must:
1. Return a CallToolResult with isError=True (NOT raise an exception).
2. Include an error message in content that mentions the unknown name.

This is a protocol-level contract: the MCP spec says tool-not-found
errors surface as `isError=True` results, not as JSON-RPC errors, so
the client can present them to the user inline with the tool-call UI.

loop_scope: all tests use "session" scope to share the mcp_inspector
fixture's session-scoped event loop (see conftest.py for rationale).
"""

from __future__ import annotations

import pytest

# All tests share the session-scoped mcp_inspector fixture, so they
# MUST run on the session-scoped event loop (loop_scope="session").
_ASYNC = pytest.mark.asyncio(loop_scope="session")


@_ASYNC
async def test_call_unknown_tool_returns_error(mcp_inspector):
    """Calling 'ppsspp_nonexistent_tool' must return isError=True."""
    result = await mcp_inspector.call_tool("ppsspp_nonexistent_tool", {})

    assert result.is_error, (
        f"expected isError=True for unknown tool, got success — content={result.content!r}"
    )


@_ASYNC
async def test_call_unknown_tool_error_mentions_name(mcp_inspector):
    """The error message should reference the unknown tool name."""
    unknown_name = "ppsspp_totally_made_up_tool_xyz"
    result = await mcp_inspector.call_tool(unknown_name, {})

    assert result.is_error
    # The error message is in content[0].text (TextContent).
    assert len(result.content) >= 1
    error_text = result.content[0].text
    # The server should mention the tool name in the error (case varies).
    assert unknown_name.lower() in error_text.lower() or "unknown" in error_text.lower(), (
        f"error message should mention the tool name or 'unknown', got: {error_text!r}"
    )


@_ASYNC
async def test_call_known_tool_does_not_set_isError(mcp_inspector):
    """Sanity check: a known tool (ppsspp_health) must NOT set isError.

    This guards against a regression where the error-detection logic
    flags ALL tool calls as errors (e.g. due to a broken output schema
    validator). If ppsspp_health is suddenly isError=True, something
    is very wrong with the server's tool dispatch.
    """
    result = await mcp_inspector.call_tool("ppsspp_health", {})

    assert not result.is_error, (
        f"ppsspp_health should not be an error, but got isError=True. content={result.content!r}"
    )
