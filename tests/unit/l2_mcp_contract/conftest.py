"""L2 MCP contract test fixtures.

L2 tests assert the MCP-facing tool contract: tool registry shape,
ToolAnnotations semantics, error code translation, and health response
fields. Anchor: MCP spec + the SDK v2 tool registry (populated by
`@mcp.tool()` decorators in `tools/*.py`) / `errors.py` (NOT violation
numbers — that's L4).

L2 tests are pure metadata tests — they do NOT call any tool function.
They inspect the registration data that the SDK uses to generate
`tools/list` responses, read back through `mcp._tool_manager._tools`
(the same single point `server.registered_tool_names()` uses).

Environment note: importing `ppsspp_dfx_mcp.server` (and the session
launcher chain) may pull Windows-only modules. When PyWin32 is not
installed (e.g. CI minimal env), we inject stub modules so the import
chain succeeds. The stubs are never called at test time (L2 tests
inspect metadata only, never spawn real Windows processes).
"""

from __future__ import annotations

# satisfy `import <mod>` statements. L2 tests never call any function
# from these modules (they inspect metadata).
import pytest

from ppsspp_dfx_mcp import server as server_mod


def _static_registry_entries() -> list[tuple[str, str, object]]:
    """(name, description, fn) entries for all statically registered tools.

    Reads back through the SDK v2 registry after `register_all_tools()`
    (decorator registration). Dynamic `ppsspp_script_*` tools are NOT
    included — they register in lifespan, which L2 metadata tests never
    run; this mirrors the pre-v2 `_TOOL_REGISTRY` (static only).
    """
    server_mod.register_all_tools()
    tools = server_mod.mcp._tool_manager._tools
    return sorted((name, tool.description or "", tool.fn) for name, tool in tools.items())


@pytest.fixture
def tool_registry() -> list[tuple[str, str, object]]:
    """Statically registered tools as (name, description, fn) 3-tuples."""
    return _static_registry_entries()


@pytest.fixture
def annotations() -> dict[str, object]:
    """ToolAnnotations per tool name, read back from the SDK registry."""
    server_mod.register_all_tools()
    tools = server_mod.mcp._tool_manager._tools
    return {name: tool.annotations for name, tool in tools.items()}


@pytest.fixture
def registry_names(tool_registry) -> set[str]:
    """Set of tool names extracted from the registry."""
    return {name for name, _, _ in tool_registry}


@pytest.fixture
def annotation_names(annotations) -> set[str]:
    """Set of tool names extracted from the annotations mapping."""
    return set(annotations.keys())
