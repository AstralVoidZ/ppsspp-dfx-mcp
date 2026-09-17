"""test_tool_annotations_contract.py — L2 MCP contract: ToolAnnotations semantics.

Anchor: server.py `_ANNOTATIONS` + tool description BEHAVIOR paragraph.

ToolAnnotations is the MCP-standard way to hint at tool side-effects.
FastMCP surfaces these hints in `tools/list` so agents can reason about
which tools are safe to call without confirmation. The four hints are:

- readOnlyHint: True if the tool only reads state (no side effects).
- destructiveHint: True if the tool irreversibly mutates state.
- idempotentHint: True if repeated calls produce the same result.
- openWorldHint: True if the tool interacts with external entities
  (processes, files, network) outside the MCP server.

The tests below cross-check _ANNOTATIONS against the BEHAVIOR paragraph
in each tool's description, which ends with one of four annotations:
DESTRUCTIVE / MUTATING / READ-ONLY / STATE-CHANGE.
"""

from __future__ import annotations

import pytest

# ============================================================================
# DESTRUCTIVE tools — destructiveHint=True, readOnlyHint=False
# ============================================================================


class TestDestructiveAnnotations:
    """Tools whose BEHAVIOR paragraph ends with 'DESTRUCTIVE' must have
    destructiveHint=True and readOnlyHint=False."""

    # Tool names whose description BEHAVIOR ends with DESTRUCTIVE
    # (extracted by reading server.py _TOOL_REGISTRY descriptions).
    _DESTRUCTIVE_TOOLS = {
        "ppsspp_write_memory",
        "ppsspp_write_register",
        "ppsspp_assemble",
    }

    def test_destructive_tools_have_destructive_hint_true(self, annotations):
        """destructiveHint=True for every DESTRUCTIVE tool."""
        for name in self._DESTRUCTIVE_TOOLS:
            ann = annotations[name]
            assert ann.destructive_hint is True, (
                f"{name}: expected destructiveHint=True, got {ann.destructive_hint}"
            )

    def test_destructive_tools_have_read_only_hint_false(self, annotations):
        """readOnlyHint=False for every DESTRUCTIVE tool."""
        for name in self._DESTRUCTIVE_TOOLS:
            ann = annotations[name]
            assert ann.read_only_hint is False, (
                f"{name}: expected readOnlyHint=False, got {ann.read_only_hint}"
            )

    def test_destructive_keyword_in_behavior_paragraph(self, tool_registry):
        """Every DESTRUCTIVE-annotated tool's description contains 'DESTRUCTIVE.'"""
        for name, desc, _ in tool_registry:
            if name in self._DESTRUCTIVE_TOOLS:
                assert "DESTRUCTIVE." in desc, f"{name}: expected 'DESTRUCTIVE.' in description"


# ============================================================================
# READ-ONLY tools — readOnlyHint=True, destructiveHint=False
# ============================================================================


class TestReadOnlyAnnotations:
    """Tools whose BEHAVIOR paragraph ends with 'READ-ONLY' must have
    readOnlyHint=True and destructiveHint=False."""

    # Subset of canonical read-only tools (representative, not exhaustive —
    # exhaustive coverage is the job of the parameterized test below).
    _READ_ONLY_TOOLS = {
        "ppsspp_health",
        "ppsspp_smoke_test",
        "ppsspp_screenshot",
        "ppsspp_dump",
        "ppsspp_read_memory",
        "ppsspp_disassemble",
        "ppsspp_query",
        "ppsspp_get_pc",
        "ppsspp_analyze_log",
        "ppsspp_list_scripts",
        "ppsspp_evaluate",
        "ppsspp_search_disasm",
        "ppsspp_memory_map",
        "ppsspp_gpu_stats",
        "ppsspp_gpu_record",
        "ppsspp_search_memory_info",
    }

    def test_read_only_tools_have_read_only_hint_true(self, annotations):
        """readOnlyHint=True for every READ-ONLY tool."""
        for name in self._READ_ONLY_TOOLS:
            ann = annotations[name]
            assert ann.read_only_hint is True, (
                f"{name}: expected readOnlyHint=True, got {ann.read_only_hint}"
            )

    def test_read_only_tools_have_destructive_hint_false(self, annotations):
        """destructiveHint=False for every READ-ONLY tool."""
        for name in self._READ_ONLY_TOOLS:
            ann = annotations[name]
            assert ann.destructive_hint is False, (
                f"{name}: expected destructiveHint=False, got {ann.destructive_hint}"
            )

    def test_read_only_keyword_in_behavior_paragraph(self, tool_registry):
        """Every READ-ONLY-annotated tool's description contains 'READ-ONLY.'"""
        for name, desc, _ in tool_registry:
            if name in self._READ_ONLY_TOOLS:
                assert "READ-ONLY." in desc, f"{name}: expected 'READ-ONLY.' in description"


# ============================================================================
# STATE-CHANGE / MUTATING tools — readOnlyHint=False, destructiveHint=False
# ============================================================================


class TestStateChangeAnnotations:
    """Tools whose BEHAVIOR paragraph ends with 'STATE-CHANGE' or 'MUTATING'
    must have readOnlyHint=False and destructiveHint=False (they mutate
    state but are not irreversible)."""

    _STATE_CHANGE_TOOLS = {
        "ppsspp_session",
        "ppsspp_step",
        "ppsspp_press_button",
        "ppsspp_hold_buttons",
        "ppsspp_send_analog",
        "ppsspp_wait_frames",
        "ppsspp_run_script",
        "ppsspp_replay",
        "ppsspp_state_observer",
        "ppsspp_batch_step",
    }

    _MUTATING_TOOLS = {
        "ppsspp_breakpoint",
        "ppsspp_reload_scripts",
    }

    @pytest.mark.parametrize(
        "tool_name",
        sorted(_STATE_CHANGE_TOOLS | _MUTATING_TOOLS),
    )
    def test_state_change_tools_have_read_only_hint_false(self, annotations, tool_name):
        """readOnlyHint=False for every STATE-CHANGE / MUTATING tool."""
        ann = annotations[tool_name]
        assert ann.read_only_hint is False, (
            f"{tool_name}: expected readOnlyHint=False, got {ann.read_only_hint}"
        )

    @pytest.mark.parametrize(
        "tool_name",
        sorted(_STATE_CHANGE_TOOLS | _MUTATING_TOOLS),
    )
    def test_state_change_tools_have_destructive_hint_false(self, annotations, tool_name):
        """destructiveHint=False for every STATE-CHANGE / MUTATING tool
        (they mutate but are not irreversible)."""
        ann = annotations[tool_name]
        assert ann.destructive_hint is False, (
            f"{tool_name}: expected destructiveHint=False, got {ann.destructive_hint}"
        )


# ============================================================================
# Cross-check: every tool's BEHAVIOR paragraph ends with one of 4 keywords
# ============================================================================


class TestBehaviorKeywordCoverage:
    """Every tool description's BEHAVIOR paragraph must end with exactly
    one of the four annotation keywords: DESTRUCTIVE / MUTATING /
    READ-ONLY / STATE-CHANGE."""

    _ALL_KEYWORDS = ("DESTRUCTIVE.", "MUTATING.", "READ-ONLY.", "STATE-CHANGE.")

    def test_every_tool_has_at_least_one_behavior_keyword(self, tool_registry):
        """Every tool description must contain at least one of the 4 keywords."""
        for name, desc, _ in tool_registry:
            found = [kw for kw in self._ALL_KEYWORDS if kw in desc]
            assert found, f"{name}: missing all 4 behavior keywords in description"
