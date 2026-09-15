"""Tools package — MCP tool functions on the SDK v2 decorator path.

Each tool module declares `@mcp.tool(...)` on its business functions
(imported by `server.register_all_tools()`); the SDK generates
inputSchema from the signatures and takes the docstring as the tool
description (TDQS format). Tool implementations only translate
exceptions to ToolError — all business logic lives in `session/`.
"""
