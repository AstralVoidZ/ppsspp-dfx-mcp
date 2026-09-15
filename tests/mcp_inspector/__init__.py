"""MCP Inspector integration tests (Phase 5).

These tests verify the MCP server's protocol-level contract by launching
a real server subprocess (stdio transport) and connecting a real
`mcp.client.session.ClientSession` to it. The server runs in fake test
mode (PPSSPP_DFX_TEST_MODE=fake) so no live PPSSPP is required — the
server substitutes a FakeTransport pre-loaded with recorded fixtures.

Test layout:
- `conftest.py` — `mcp_inspector` fixture (session-scoped ClientSession)
- `test_tools_list.py` — verify list_tools returns 30+ tools
- `test_tools_call.py` — verify ppsspp_health returns status="ok"
- `test_tools_call_unknown.py` — verify unknown tool returns MCP error
- `test_server_lifecycle.py` — verify fixture manages subprocess cleanly
"""
