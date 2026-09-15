"""R15 fix tests: ToolError renders as "[CODE] message" on the wire.

Probe (mcp 2.1.1): the SDK renders str(exc) into TextContent
(mcpserver/server.py:441) — CallToolResult has no structured error-data
channel, so the machine-readable code was invisible to agents. The fix
overrides ToolError.__str__ at a single point; raise sites keep their own
messages untouched.
"""

from __future__ import annotations

import asyncio

import pytest

from ppsspp_dfx_mcp.errors import (
    SessionBusy,
    StepNoAdvanceError,
    ToolError,
    to_tool_error,
)
from ppsspp_dfx_mcp.tools._common import translate_tool_errors


def test_str_prefixes_default_internal_code():
    assert str(ToolError("boom")) == "[INTERNAL] boom"


def test_str_prefixes_subclass_code():
    assert str(SessionBusy("busy now")) == "[SESSION_BUSY] busy now"


def test_custom_code_argument_is_prefixed():
    err = ToolError("plain", code="PROTECTED_ADDRESS")
    assert str(err) == "[PROTECTED_ADDRESS] plain"


def test_to_tool_error_wraps_timeout_with_prefix():
    wrapped = to_tool_error(TimeoutError("rpc died"))
    assert str(wrapped).startswith("[WS_TIMEOUT] ")
    assert "rpc died" in str(wrapped)


def test_to_tool_error_preserves_toolerror_without_double_prefix():
    original = StepNoAdvanceError("no advance")
    out = to_tool_error(original)
    assert out is original
    assert str(out) == "[STEP_NO_ADVANCE] no advance"
    # Wrapping again (tools re-raising through translate_tool_errors)
    # must not stack prefixes.
    out2 = to_tool_error(out)
    assert str(out2).count("[STEP_NO_ADVANCE]") == 1


@pytest.mark.asyncio
async def test_translate_tool_errors_output_carries_prefix():
    @translate_tool_errors
    async def _boom() -> None:
        raise RuntimeError("raw failure")

    with pytest.raises(ToolError) as exc_info:
        await _boom()
    text = str(exc_info.value)
    assert text.startswith("[")
    assert "]" in text
    assert "raw failure" in text
