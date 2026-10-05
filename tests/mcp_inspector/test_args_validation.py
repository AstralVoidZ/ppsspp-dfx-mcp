"""End-to-end: tool-argument validation errors are normalized (FR-002 / G-2).

Runs over a REAL stdio JSON-RPC session (`mcp_inspector` launches the
production entry point `python -m ppsspp_dfx_mcp`). This is the only layer
that exercises `tool_error_middleware`: an in-process `mcp.call_tool(...)`
bypasses the wire-protocol dispatch and therefore never reaches middleware.

Before the fix, an invalid argument reached the SDK's own validation and
came back as a raw pydantic dump:
    Error executing tool ppsspp_breakpoint: 1 validation error for
    ppsspp_breakpointArguments
    session_id
      Input should be a valid string [type=string_type, input_value=123, ...]
    For further information visit https://errors.pydantic.dev/2.x/v/string_type

After the fix it is a single actionable line carrying only `[ARGS_INVALID]`.
"""

from __future__ import annotations

import pytest

_ASYNC = pytest.mark.asyncio(loop_scope="session")

# Pydantic internals that must NEVER reach the client.
_FORBIDDEN = (
    "validation error for",
    "[type=",
    "errors.pydantic.dev",
    "Arguments",
)


@_ASYNC
@pytest.mark.parametrize(
    "tool_name",
    ["ppsspp_batch_step", "ppsspp_write_register", "ppsspp_breakpoint"],
)
async def test_type_error_is_normalized(mcp_inspector, tool_name):
    """A type error on `session_id` returns [ARGS_INVALID], not a pydantic dump.

    "How it fails": without `tool_error_middleware` the response text is
    the SDK's `Error executing tool ...: 1 validation error for
    <tool>Arguments ...` dump, so both the `[ARGS_INVALID]` presence
    assertion and the forbidden-substring assertions go red.
    """
    result = await mcp_inspector.call_tool(tool_name, {"session_id": 123})
    assert result.is_error, f"{tool_name}: expected an error result"
    text = result.content[0].text if result.content else ""
    assert "[ARGS_INVALID]" in text, f"{tool_name}: missing [ARGS_INVALID]: {text!r}"
    for bad in _FORBIDDEN:
        assert bad not in text, f"{tool_name}: leaked pydantic detail {bad!r}: {text!r}"


@_ASYNC
async def test_valid_args_still_succeed(mcp_inspector):
    """FR-002 (two-direction): a valid call is NOT short-circuited.

    `ppsspp_health` is a pure liveness probe needing no session. If the
    middleware over-rejected (e.g. its no-arg path raised), this call
    would come back is_error; it must succeed instead.
    """
    result = await mcp_inspector.call_tool("ppsspp_health", {})
    assert not result.is_error, f"ppsspp_health unexpectedly failed: {result.content!r}"
