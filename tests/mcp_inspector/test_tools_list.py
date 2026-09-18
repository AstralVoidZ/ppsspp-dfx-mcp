"""Test: list_tools returns the full static tool set.

Verifies the MCP protocol layer (ClientSession → server → list_tools)
returns all expected static tools. The expected set is derived from the
tool-surface baseline (tests/unit/l2_mcp_contract/tool_surface_baseline.json)
— the same byte-level lock used by the L2 contract tests — so this file
cannot drift from the registered surface.

Dynamic ppsspp_script_<name> tools are manifest-dependent (0..N) and are
NOT part of the expected set; the count assertions use >= for that reason.

loop_scope: all tests use "session" scope to share the mcp_inspector
fixture's session-scoped event loop (see conftest.py for rationale).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_BASELINE_PATH = (
    Path(__file__).resolve().parents[1] / "unit" / "l2_mcp_contract" / "tool_surface_baseline.json"
)


def _expected_static_tools() -> frozenset[str]:
    baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    return frozenset(baseline["tools"].keys())


EXPECTED_STATIC_TOOLS: frozenset[str] = _expected_static_tools()
EXPECTED_COUNT = len(EXPECTED_STATIC_TOOLS)

# All tests share the session-scoped mcp_inspector fixture, so they
# MUST run on the session-scoped event loop (loop_scope="session").
_ASYNC = pytest.mark.asyncio(loop_scope="session")


@_ASYNC
async def test_list_tools_returns_all_static_tools(mcp_inspector):
    """list_tools must return at least the full static tool set.

    Dynamic ppsspp_script_<name> tools may push the count higher if the
    manifest exposes scripts, but the static set must always be present.
    """
    result = await mcp_inspector.list_tools()
    tool_names = {t.name for t in result.tools}

    assert len(result.tools) >= EXPECTED_COUNT, (
        f"expected >={EXPECTED_COUNT} tools, got {len(result.tools)}: {sorted(tool_names)}"
    )


@_ASYNC
async def test_list_tools_includes_core_set(mcp_inspector):
    """Core protocol tools (health/session) must always be present."""
    result = await mcp_inspector.list_tools()
    tool_names = {t.name for t in result.tools}

    missing = {"ppsspp_health", "ppsspp_session"} - tool_names
    assert not missing, f"core tools missing: {sorted(missing)}"


@_ASYNC
async def test_list_tools_includes_all_static_tools(mcp_inspector):
    """Every tool in the baseline surface must be present."""
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
