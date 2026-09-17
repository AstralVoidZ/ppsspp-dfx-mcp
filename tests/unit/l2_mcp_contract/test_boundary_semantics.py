"""R6/R7 (design_ppsspp_dfx_mcp_test_refactor_v1 §R6/§R7): boundary
semantics table + output budget guards.

R6 freezes the interface boundary semantics observed and ratified in the
real-MCP verification (report F-6/F-9/F-11/F-14) as an executable table:
each row states the expected verdict (accept / reject / clamp) so a
semantic change MUST update the table (and cite the F-number) to go green.

R7 pins the output budget: single memory reads are capped (F-6) so no
single call can return an unbounded response.

No-session rows run full-stack via MCPServer.call_tool; write-path rows
use a mocked session client (validation is client-side, no PPSSPP).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp import server as server_mod
from ppsspp_dfx_mcp.errors import ToolError

server_mod.register_all_tools()
pytestmark = pytest.mark.asyncio

# (tool, args, verdict, fragment) — verdict: "accept" | "reject"
# Fragment pins the message identity; verdict pins accept/reject semantics.
BOUNDARY_TABLE: list[tuple[str, dict, str, str]] = [
    # convert_address (F-14: conversion semantics + hex validation)
    ("ppsspp_convert_address", {"address": "0x0", "mode": "ida_to_ppsspp"}, "accept", ""),
    ("ppsspp_convert_address", {"address": "0x08804000", "mode": "ppsspp_to_ida"}, "accept", ""),
    (
        "ppsspp_convert_address",
        {"address": "0x08000000", "mode": "ppsspp_to_ida"},
        "reject",
        "is negative",
    ),
    # session/health read-only paths
    ("ppsspp_session_list", {}, "accept", ""),
    ("ppsspp_health", {}, "accept", ""),
    # schema-level rejections (R2 pins the enum; here pin the wire text)
    (
        "ppsspp_read_memory",
        {"action": "read_u64", "address": "0x08804000"},
        "reject",
        "Input should be",
    ),
]


async def _client_view(tool: str, args: dict) -> tuple[bool, str]:
    try:
        result = await server_mod.mcp.call_tool(tool, dict(args), None)
    except Exception as e:
        return True, str(e)
    err = getattr(result, "is_error", False)
    text = "\n".join(getattr(c, "text", "") for c in getattr(result, "content", None) or [])
    return bool(err), text


class TestBoundarySemantics:
    @pytest.mark.parametrize(("tool", "args", "verdict", "fragment"), BOUNDARY_TABLE)
    async def test_boundary_row(self, tool: str, args: dict, verdict: str, fragment: str) -> None:
        is_error, text = await _client_view(tool, args)
        if verdict == "accept":
            assert not is_error, f"{tool} {args}: expected accept, got {text[:120]}"
        else:
            assert is_error, f"{tool} {args}: expected reject (semantic table)"
            assert fragment in text, (
                f"{tool} {args}: reject message must contain {fragment!r} "
                f"(got {text[:150]!r}) — update the F-number citation when "
                f"changing this row"
            )


class TestOutputBudget:
    """R7: single-read cap (F-6) — no unbounded responses."""

    async def test_read_bytes_over_cap_rejected(self, monkeypatch) -> None:
        mock_client = AsyncMock()

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr("ppsspp_dfx_mcp.tools.memory.session_client", fake_session_client)
        with pytest.raises(ToolError) as exc_info:
            await server_mod.mcp._tool_manager._tools["ppsspp_read_memory"].fn(
                action="read_bytes",
                address="0x08804000",
                size=1048576,
                session_id="s",
            )
        assert "single-read cap" in str(exc_info.value)
        mock_client.read_bytes.assert_not_awaited()

    async def test_read_bytes_at_cap_allowed(self, monkeypatch) -> None:
        mock_client = AsyncMock()
        mock_client.read_bytes.return_value = b"\x00" * 65536

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr("ppsspp_dfx_mcp.tools.memory.session_client", fake_session_client)
        tool_fn = server_mod.mcp._tool_manager._tools["ppsspp_read_memory"].fn
        result = await tool_fn(
            action="read_bytes",
            address="0x08804000",
            size=65536,
            session_id="s",
        )
        assert result["size"] == 65536
