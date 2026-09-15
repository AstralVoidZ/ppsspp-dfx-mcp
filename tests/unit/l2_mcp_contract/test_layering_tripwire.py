"""Layering contract tripwire (architecture review v1 §4.1).

Static import-graph assertion: lower layers must never import the tools
layer (and the pure contract layers must stay leaf-ish). This is the
permanent guard for the P2 layering-inversion convergence — the three
inversions it was written against (core/batch_jobs, core/stepping,
service/debug_client importing tools._common / session_manager) must
never regrow.

Run (from mcps/ppsspp-dfx-mcp/):
    <venv python> -m pytest tests/unit/l2_mcp_contract/test_layering_tripwire.py -q
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "ppsspp_dfx_mcp"

# forbidden[importing_layer] = {imported_layers}
FORBIDDEN: dict[str, frozenset[str]] = {
    "core": frozenset({"tools"}),
    "service": frozenset({"tools"}),
    "models": frozenset({"tools", "session", "service"}),
    "views": frozenset({"tools", "session", "service"}),
}


def _package_imports(tree: ast.AST) -> set[str]:
    """All ppsspp_dfx_mcp submodule names imported anywhere in the tree
    (top-level AND function bodies — lazy imports count too)."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "ppsspp_dfx_mcp" and len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.startswith("ppsspp_dfx_mcp."):
                parts = node.module.split(".")
                if len(parts) > 1:
                    found.add(parts[1])
            elif node.level and node.module and node.module.split(".")[0] in FORBIDDEN:
                # relative import like `from .. import tools` (defensive)
                found.add(node.module.split(".")[0])
    return found


def test_lower_layers_never_import_tools():
    violations: list[str] = []
    for layer, forbidden in FORBIDDEN.items():
        layer_dir = _SRC / layer
        for py in layer_dir.rglob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"))
            imported = _package_imports(tree)
            bad = imported & forbidden
            if bad:
                violations.append(f"{layer}/{py.name} imports {sorted(bad)}")
    assert not violations, (
        "layering violation — lower layers importing upper layers: "
        f"{violations}. Move the shared symbol down into core "
        "(see CONTRIBUTING.md — tools may import core, never the reverse)."
    )


def test_tripwire_catches_a_real_violation():
    """Guard the guard: the scanner must flag a planted violation."""
    tree = ast.parse("from ppsspp_dfx_mcp.tools._common import X")
    assert "tools" in _package_imports(tree)
    tree = ast.parse("from ppsspp_dfx_mcp.core import proc")
    assert "tools" not in _package_imports(tree)
