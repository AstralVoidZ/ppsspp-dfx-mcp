"""test_resources_prompts_contract.py — L2 MCP contract: Resources + Prompts.

Anchor: delivery U-02 (Resources + Prompts primitives) +
src/ppsspp_dfx_mcp/resources.py + prompts.py.

L2 tests are pure metadata: they verify the registration data the SDK
uses for resources/list and prompts/list, plus the tools/list ordering
stability required by the 2026-07-28 spec (deterministic order for
client-side caching). They do NOT read live resources — that needs a
session and belongs to L3 / mcp_inspector protocol tests.
"""

from __future__ import annotations

import asyncio

import pytest

from ppsspp_dfx_mcp import server as server_mod


@pytest.fixture(scope="module")
def _registered() -> None:
    server_mod.register_all_tools()


@pytest.fixture
def resources(_registered):
    return asyncio.run(server_mod.mcp.list_resources())


@pytest.fixture
def prompts(_registered):
    return asyncio.run(server_mod.mcp.list_prompts())


# ============================================================================
# Resources — delivery U-02 snapshots
# ============================================================================


class TestSnapshotResourcesRegistered:
    """The two snapshot resources are registered with usable metadata."""

    _EXPECTED_URIS = {"ppsspp://game-state", "ppsspp://registers"}

    def test_snapshot_resources_present(self, resources):
        uris = {str(r.uri) for r in resources}
        missing = self._EXPECTED_URIS - uris
        assert not missing, f"snapshot resources missing: {sorted(missing)}"

    def test_resource_metadata_non_empty(self, resources):
        """Each snapshot resource carries name + description."""
        by_uri = {str(r.uri): r for r in resources}
        for uri in self._EXPECTED_URIS:
            r = by_uri.get(uri)
            assert r is not None, f"{uri} not registered"
            assert r.name, f"{uri}: empty name"
            assert r.description, f"{uri}: empty description"


# ============================================================================
# Prompts — memory-breakpoint-wizard
# ============================================================================


class TestMemoryBreakpointWizard:
    """The wizard prompt is registered with the expected argument schema."""

    def test_prompt_present(self, prompts):
        names = {p.name for p in prompts}
        assert "memory-breakpoint-wizard" in names

    def test_prompt_arguments(self, prompts):
        by_name = {p.name: p for p in prompts}
        args = {a.name: a for a in (by_name["memory-breakpoint-wizard"].arguments or [])}
        assert "address" in args, "wizard must accept an address argument"
        assert "size" in args and "purpose" in args, (
            "wizard optional arguments (size / purpose) missing"
        )
        assert args["address"].required is True, "address must be required"
        assert args["size"].required is False, "size must be optional (default 4)"
        assert args["purpose"].required is False, "purpose must be optional"

    def test_prompt_description_non_empty(self, prompts):
        by_name = {p.name: p for p in prompts}
        assert by_name["memory-breakpoint-wizard"].description


# ============================================================================
# tools/list deterministic ordering — spec 2026-07-28
# ============================================================================


class TestToolsListOrdering:
    """tools/list must return tools in a deterministic order.

    Anchor: MCP spec 2026-07-28 minor change 3 — "Servers SHOULD return
    tools from tools/list in a deterministic order to enable client-side
    caching and improve LLM prompt cache hit rates."

    The SDK v2 registry preserves decorator registration order (module
    import order is fixed by _TOOL_MODULE_NAMES), so repeated calls must
    return identical sequences.
    """

    def test_tools_list_order_stable_across_calls(self, _registered):
        lists = [
            [t.name for t in asyncio.run(server_mod.mcp.list_tools())]
            for _ in range(3)
        ]
        assert lists[0] == lists[1] == lists[2], (
            "tools/list order differs between calls — client-side tool "
            "caching and prompt cache hits would thrash"
        )
        # Note: the deterministic order is decorator registration order
        # (fixed module list in server._TOOL_MODULE_NAMES). We do NOT
        # name-sort — the spec requires determinism, not sortedness.


# ============================================================================
# P7 — memory-trace-wizard (H2, 2026-09-07)
# ============================================================================


class TestMemoryTraceWizardRegistered:
    """The H1 tracing workflow is exposed as an on-demand prompt."""

    def test_trace_wizard_present(self, prompts):
        names = {p.name for p in prompts}
        assert "memory-trace-wizard" in names

    def test_trace_wizard_arguments(self, prompts):
        prompt = next(p for p in prompts if p.name == "memory-trace-wizard")
        arg_names = {a.name for a in (prompt.arguments or [])}
        assert {"address"} <= arg_names
        assert prompt.description

    def test_trace_wizard_renders_workflow(self, _registered):
        from ppsspp_dfx_mcp.prompts import memory_trace_wizard

        text = memory_trace_wizard("0x08A0D000")
        assert "ppsspp_trace_memory_access" in text
        assert "ppsspp_wait_breakpoint" in text
        assert "0x08A0D000" in text
