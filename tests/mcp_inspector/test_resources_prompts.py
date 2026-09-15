"""Protocol-level tests for Resources + Prompts (fake-mode MCP server).

Runs against the session-scoped fake-mode server subprocess via the
`mcp_inspector` ClientSession fixture — the real stdio JSON-RPC path,
not in-process calls. Verifies:

1. resources/list advertises the two snapshot resources (delivery U-02).
2. prompts/get renders memory-breakpoint-wizard with the address woven in.
3. resources/read of a snapshot without an active session returns an
   error result (ResourceError path) — fake mode starts no session here.
"""

from __future__ import annotations

import json

import pytest

loop_scope = "session"  # share the mcp_inspector session-scoped loop


@pytest.mark.asyncio(loop_scope=loop_scope)
async def test_resources_list_advertises_snapshots(mcp_inspector):
    result = await mcp_inspector.list_resources()
    uris = {str(r.uri) for r in result.resources}
    assert {"ppsspp://game-state", "ppsspp://registers"} <= uris, (
        f"snapshot resources missing from resources/list: {sorted(uris)}"
    )


@pytest.mark.asyncio(loop_scope=loop_scope)
async def test_prompt_wizard_renders_with_address(mcp_inspector):
    result = await mcp_inspector.get_prompt(
        "memory-breakpoint-wizard",
        {"address": "0x09000000", "purpose": "catch save-flag writes"},
    )
    assert result.messages, "prompt rendered no messages"
    text = result.messages[0].content.text
    assert "0x09000000" in text, "address not woven into the workflow"
    assert "catch save-flag writes" in text, "purpose not woven in"
    assert "ppsspp_breakpoint" in text, "workflow must reference the tool"
    assert "mem_remove" in text, "workflow must include cleanup"


@pytest.mark.asyncio(loop_scope=loop_scope)
async def test_read_snapshot_without_session_errors(mcp_inspector):
    """Snapshot read with no active session → MCPError (clean refusal).

    The server converts the ResourceError into a JSON-RPC error; the
    client surfaces it as McpError. Either way the contract outcome is:
    refused cleanly, no hang, no junk payload.
    """
    from mcp.shared.exceptions import MCPError

    with pytest.raises(MCPError) as exc_info:
        await mcp_inspector.read_resource("ppsspp://game-state")
    assert "no active PPSSPP session" in str(exc_info.value)


@pytest.mark.asyncio(loop_scope=loop_scope)
async def test_prompt_wizard_rejects_missing_address(mcp_inspector):
    """Calling the wizard without its required argument → clean error."""
    from mcp.shared.exceptions import MCPError

    with pytest.raises(MCPError):
        await mcp_inspector.get_prompt("memory-breakpoint-wizard", {})
