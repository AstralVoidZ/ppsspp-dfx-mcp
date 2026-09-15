"""test_tool_registry_contract.py — L2 MCP contract: tool registry shape.

Anchor: server.py `_TOOL_REGISTRY` + `_CORE_TOOLS` +
`_assert_core_tools` startup validation + TDQS description format
(specs/tdqs-descriptions/spec.md — PURPOSE / USAGE / BEHAVIOR / RETURNS).

L2 tests are pure metadata: they verify the registration tables that
FastMCP reads to generate `tools/list` responses. They do NOT call any
tool function — that's the job of L1 (forwarding) / L3 (orchestration).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable

import pytest
from mcp.types import ToolAnnotations

from ppsspp_dfx_mcp import server as server_mod


# ============================================================================
# Registry size & naming
# ============================================================================


class TestRegistrySize:
    """The registry must contain all core tools + be non-empty.

    Anchor: server.py — `_CORE_TOOLS` (health / session / session_list)
    + `_register_tools()` runs at module import time. The full tool
    set is whatever `_register_tools()` registers; startup validation
    only checks the core subset (see `_assert_core_tools`).

    Design: tool count is NOT locked to a phase constant — adding a
    new tool only requires appending to `_TOOL_REGISTRY` + adding a
    ToolAnnotations entry. The registry size assertion is a sanity
    floor (must include all core tools), not a strict ceiling.
    """

    def test_registry_includes_core_tools(self, registry_names):
        """All _CORE_TOOLS must be present in registry (liveness floor)."""
        missing = server_mod._CORE_TOOLS - registry_names
        assert not missing, (
            f"core tools missing from registry: {sorted(missing)}"
        )

    def test_registry_is_non_empty(self, tool_registry):
        """Registry must contain at least the 3 core tools."""
        assert len(tool_registry) >= len(server_mod._CORE_TOOLS), (
            f"registry too small: {len(tool_registry)} tools, "
            f"expected at least {len(server_mod._CORE_TOOLS)}"
        )

    def test_no_duplicate_tool_names(self, tool_registry):
        """Each tool name appears exactly once in the registry."""
        names = [name for name, _, _ in tool_registry]
        assert len(names) == len(set(names)), (
            f"duplicate tool names: {sorted([n for n in names if names.count(n) > 1])}"
        )


# ============================================================================
# Registry ↔ _ANNOTATIONS coverage
# ============================================================================


class TestRegistryAnnotationCoverage:
    """Every registry tool must have a ToolAnnotations entry, and vice versa."""

    def test_every_registry_tool_has_annotation(
        self, registry_names, annotation_names
    ):
        """_ANNOTATIONS must cover every tool in _TOOL_REGISTRY."""
        missing = registry_names - annotation_names
        assert not missing, f"tools without annotations: {sorted(missing)}"

    def test_every_annotation_has_registry_entry(
        self, registry_names, annotation_names
    ):
        """_ANNOTATIONS must not contain entries not in _TOOL_REGISTRY."""
        orphan = annotation_names - registry_names
        assert not orphan, f"orphan annotations: {sorted(orphan)}"

    def test_annotation_values_are_toolannotations(self, annotations):
        """Each _ANNOTATIONS value must be a ToolAnnotations instance."""
        for name, ann in annotations.items():
            assert isinstance(ann, ToolAnnotations), (
                f"{name}: expected ToolAnnotations, got {type(ann).__name__}"
            )


# ============================================================================
# Registry entry shape
# ============================================================================


class TestRegistryEntryShape:
    """Each registry entry is a (name, description, fn) 3-tuple."""

    def test_entry_is_three_tuple(self, tool_registry):
        """Every entry must be a 3-tuple (name, description, fn)."""
        for entry in tool_registry:
            assert isinstance(entry, tuple) and len(entry) == 3, (
                f"entry must be 3-tuple, got {type(entry).__name__} len={len(entry) if hasattr(entry, '__len__') else 'n/a'}"
            )

    def test_entry_name_is_str(self, tool_registry):
        """Entry name must be a non-empty string."""
        for name, _, _ in tool_registry:
            assert isinstance(name, str) and name, (
                f"tool name must be non-empty str, got {name!r}"
            )

    def test_entry_description_is_str(self, tool_registry):
        """Entry description must be a non-empty string."""
        for _, desc, _ in tool_registry:
            assert isinstance(desc, str) and desc, (
                f"tool description must be non-empty str, got {desc!r}"
            )

    def test_entry_fn_is_callable(self, tool_registry):
        """Entry fn must be a callable (async function or wrapper)."""
        for name, _, fn in tool_registry:
            assert callable(fn), (
                f"{name}: fn must be callable, got {type(fn).__name__}"
            )


# ============================================================================
# TDQS description format
# ============================================================================


class TestToolDescriptionTdqsFormat:
    """Each tool description must follow the TDQS 4-paragraph format.

    Anchor: specs/tdqs-descriptions/spec.md — PURPOSE / USAGE / BEHAVIOR /
    RETURNS, in that order. Each paragraph header appears as an uppercase
    keyword followed by a colon.
    """

    _TDQS_HEADERS = ("PURPOSE:", "USAGE:", "BEHAVIOR:", "RETURNS:")

    @pytest.mark.parametrize("header", _TDQS_HEADERS)
    def test_each_tdqs_header_present(self, tool_registry, header):
        """Every tool description must contain all 4 TDQS headers."""
        for name, desc, _ in tool_registry:
            assert header in desc, (
                f"{name}: missing TDQS header {header!r} in description"
            )

    def test_tool_tdqs_headers_in_order(self, tool_registry):
        """TDQS headers must appear in PURPOSE → USAGE → BEHAVIOR → RETURNS order."""
        for name, desc, _ in tool_registry:
            positions = [desc.find(h) for h in self._TDQS_HEADERS]
            assert all(p >= 0 for p in positions), (
                f"{name}: missing at least one TDQS header"
            )
            assert positions == sorted(positions), (
                f"{name}: TDQS headers out of order: positions={positions}"
            )


# ============================================================================
# _assert_core_tools startup validation
# ============================================================================


class TestAssertCoreTools:
    """_assert_core_tools startup validation.

    Anchor: server.py `_assert_core_tools` docstring — verifies the
    _CORE_TOOLS subset (health / session / session_list) is present
    in _TOOL_REGISTRY at startup. Replaces the deprecated
    `_assert_tool_registration(phase=N)` mechanism: no phase counter
    to bump, no EXPECTED_TOOLS_PHASE* constants to maintain.
    """

    def test_assert_core_tools_passes(self):
        """_assert_core_tools() does not raise when registry has all core tools."""
        server_mod._assert_core_tools()

    def test_assert_core_tools_raises_on_missing(self, monkeypatch):
        """_assert_core_tools() raises RuntimeError when a core tool is missing."""
        # Simulate a missing core tool by temporarily replacing the registry
        # read helper with a view that lacks ppsspp_health.
        original_names = server_mod.registered_tool_names()
        monkeypatch.setattr(
            server_mod,
            "registered_tool_names",
            lambda: original_names - {"ppsspp_health"},
        )
        with pytest.raises(RuntimeError, match="core tools missing from registry"):
            server_mod._assert_core_tools()

    def test_core_tools_subset_is_three(self):
        """_CORE_TOOLS contains exactly the 3 liveness-critical tools."""
        assert server_mod._CORE_TOOLS == {
            "ppsspp_health",
            "ppsspp_session",
            "ppsspp_session_list",
        }


# ============================================================================
# Description purity guards
# ============================================================================


class TestDescriptionPurity:
    """Tool descriptions must be free of internal identifiers and impl details.

    tdqs-descriptions/spec.md — purity requirements.
    """

    _FORBIDDEN_ID_PATTERNS = [
        (r"spike\s+U\d", "spike reference"),
        (r"\bD-\d{2}\b", "defect ID"),
        (r"\bN-\d{2}\b", "note ID"),
        (r"\bP\d-\d{2}\b", "phase-task ID"),
    ]

    _IMPL_MECHANISM_BLACKLIST = [
        "bytes.find",
        "4KB chunk",
        "output_type='uri'",
        "client-side poller",
        "with_stepping",
        "client.texture",
        "data URI",
    ]

    def test_no_internal_ids(self, tool_registry):
        """No tool description or Field description shall contain internal IDs."""
        import re
        from pathlib import Path

        violations = []
        for name, desc, _ in tool_registry:
            for pattern, label in self._FORBIDDEN_ID_PATTERNS:
                matches = re.findall(pattern, desc)
                if matches:
                    violations.append(
                        f"{name}: {label} {matches}"
                    )

        tools_dir = Path(server_mod.__file__).parent / "tools"
        for py_file in sorted(tools_dir.glob("*.py")):
            text = py_file.read_text(encoding="utf-8")
            for m in re.finditer(
                r'description=\(?\s*"((?:[^"\\]|\\.)*)"', text
            ):
                field_desc = m.group(1)
                for pattern, label in self._FORBIDDEN_ID_PATTERNS:
                    if re.search(pattern, field_desc):
                        violations.append(
                            f"{py_file.name}: {label} in Field description"
                        )

        assert not violations, (
            "Internal identifiers found in descriptions:\n"
            + "\n".join(violations)
        )

    def test_session_id_wording(self):
        """All session_id Field descriptions shall start with 'Active session ID'."""
        import re
        from pathlib import Path

        tools_dir = Path(server_mod.__file__).parent / "tools"
        violations = []
        for py_file in sorted(tools_dir.glob("*.py")):
            text = py_file.read_text(encoding="utf-8")
            for m in re.finditer(
                r"session_id:\s*Annotated\[[\s\S]*?"
                r'Field\(\s*(?:default=[^,]*,\s*)?'
                r'description=\(?\s*"([^"]*)"',
                text,
            ):
                desc = m.group(1)
                if not desc.startswith("Active session ID") and not desc.startswith("Optional session ID"):
                    violations.append(
                        f"{py_file.name}: session_id desc={desc!r}"
                    )
        assert not violations, (
            "session_id Field descriptions not starting with "
            "'Active session ID':\n" + "\n".join(violations)
        )

    def test_behavior_no_impl_mechanism(self, tool_registry):
        """BEHAVIOR paragraphs shall not contain implementation mechanisms."""
        violations = []
        for name, desc, _ in tool_registry:
            behavior_start = desc.find("BEHAVIOR:")
            returns_start = desc.find("RETURNS:")
            if behavior_start == -1:
                continue
            behavior_text = (
                desc[behavior_start:returns_start]
                if returns_start > behavior_start
                else desc[behavior_start:]
            )
            for keyword in self._IMPL_MECHANISM_BLACKLIST:
                if keyword in behavior_text:
                    violations.append(f"{name}: '{keyword}' in BEHAVIOR")
        assert not violations, (
            "Implementation mechanisms found in BEHAVIOR paragraphs:\n"
            + "\n".join(violations)
        )
