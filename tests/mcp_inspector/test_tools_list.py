"""Test: list_tools returns the full 33+ tool set.

Verifies the MCP protocol layer (ClientSession → server → list_tools)
returns all expected static tools. Anchors:
- Phase 1 (3): ppsspp_health / ppsspp_session / ppsspp_session_list
- Phase 2 (16): ppsspp_smoke_test / ppsspp_screenshot / etc.
- Phase 3 (3): ppsspp_list_scripts / ppsspp_run_script / ppsspp_reload_scripts
- Phase 4 (5): ppsspp_write_register / ppsspp_evaluate / etc.
- Phase 5 (3): ppsspp_gpu_stats / ppsspp_gpu_record / ppsspp_memory_info_search
- Phase 6 (1): ppsspp_replay (OpenSpec add-replay-tools)
- Phase 7 (2): ppsspp_state_observer / ppsspp_batch_step (add-replay-tools P2)

Total: 33 static tools (+ N dynamic ppsspp_script_<name> if manifest
exposes any). This test only checks the static set — dynamic script
tools are manifest-dependent and may be 0.

Note: the static tool set is whatever _register_tools() in server.py
registers. There is no "phase counter" to bump — adding a new tool
only requires appending to _TOOL_REGISTRY + adding ToolAnnotations.
Update EXPECTED_STATIC_TOOLS below when you add a tool.

loop_scope: all tests use "session" scope to share the mcp_inspector
fixture's session-scoped event loop (see conftest.py for rationale).
"""

from __future__ import annotations

import pytest

# Static tool set (from server.py _TOOL_REGISTRY). Keep in sync with
# the server registration — when you add a tool, append it here. The
# test asserts ALL of these are present (subset check, not strict
# equality, so dynamic script tools don't break it).
EXPECTED_STATIC_TOOLS: frozenset[str] = frozenset(
    {
        # Phase 1 (3)
        "ppsspp_health",
        "ppsspp_session",
        "ppsspp_session_list",
        # Phase 2 (16)
        "ppsspp_smoke_test",
        "ppsspp_screenshot",
        "ppsspp_dump_texture",
        "ppsspp_read_memory",
        "ppsspp_write_memory",
        "ppsspp_disassemble",
        "ppsspp_breakpoint",
        "ppsspp_step",
        "ppsspp_query",
        "ppsspp_get_pc",
        "ppsspp_press_button",
        "ppsspp_hold_buttons",
        "ppsspp_send_analog",
        "ppsspp_wait_frames",
        "ppsspp_analyze_log",
        "ppsspp_convert_address",
        # Phase 3 (3)
        "ppsspp_list_scripts",
        "ppsspp_run_script",
        "ppsspp_reload_scripts",
        # Phase 4 (5)
        "ppsspp_write_register",
        "ppsspp_evaluate",
        "ppsspp_assemble",
        "ppsspp_search_disasm",
        "ppsspp_memory_map",
        # Phase 5 (3)
        "ppsspp_gpu_stats",
        "ppsspp_gpu_record",
        "ppsspp_memory_info_search",
        # Phase 6 (1, OpenSpec add-replay-tools)
        "ppsspp_replay",
        # Phase 7 (2, OpenSpec add-replay-tools P2)
        "ppsspp_state_observer",
        "ppsspp_batch_step",
    }
)

# All tests share the session-scoped mcp_inspector fixture, so they
# MUST run on the session-scoped event loop (loop_scope="session").
_ASYNC = pytest.mark.asyncio(loop_scope="session")


@_ASYNC
async def test_list_tools_returns_at_least_33_static_tools(mcp_inspector):
    """list_tools must return at least 33 static tools.

    Dynamic ppsspp_script_<name> tools may push the count higher if the
    manifest exposes scripts, but the 33 static tools must always be
    present.
    """
    result = await mcp_inspector.list_tools()
    tool_names = {t.name for t in result.tools}

    assert len(result.tools) >= 33, (
        f"expected >=33 tools, got {len(result.tools)}: {sorted(tool_names)}"
    )


@_ASYNC
async def test_list_tools_includes_phase1_set(mcp_inspector):
    """Phase 1 tools (health/session/session_list) must always be present."""
    result = await mcp_inspector.list_tools()
    tool_names = {t.name for t in result.tools}

    missing = {"ppsspp_health", "ppsspp_session", "ppsspp_session_list"} - tool_names
    assert not missing, f"Phase 1 tools missing: {sorted(missing)}"


@_ASYNC
async def test_list_tools_includes_all_static_tools(mcp_inspector):
    """All 33 static tools from EXPECTED_STATIC_TOOLS must be present."""
    result = await mcp_inspector.list_tools()
    tool_names = {t.name for t in result.tools}

    missing = EXPECTED_STATIC_TOOLS - tool_names
    assert not missing, f"{len(missing)} static tools missing: {sorted(missing)}"


@_ASYNC
async def test_list_tools_each_has_name_and_description(mcp_inspector):
    """Every tool must have a non-empty name and description."""
    result = await mcp_inspector.list_tools()

    for tool in result.tools:
        assert tool.name, f"tool has empty name: {tool!r}"
        assert tool.description, f"tool {tool.name!r} has empty description"


@_ASYNC
async def test_list_tools_health_has_read_only_annotation(mcp_inspector):
    """ppsspp_health must declare itself read-only (ToolAnnotations)."""
    result = await mcp_inspector.list_tools()
    health = next((t for t in result.tools if t.name == "ppsspp_health"), None)

    assert health is not None, "ppsspp_health not in tool list"
    assert health.annotations is not None, (
        "ppsspp_health has no annotations (expected readOnlyHint=True)"
    )
    assert health.annotations.read_only_hint is True, (
        f"ppsspp_health.read_only_hint should be True, got {health.annotations.read_only_hint}"
    )
