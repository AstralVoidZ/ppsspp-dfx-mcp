#!/usr/bin/env python
"""A19 gate — tool-function length limit with frozen, only-shrink exemptions.

Scans every ``tools/*.py`` module for functions decorated with ``@mcp.tool``
and measures the **function body** (the first body statement through the
function's last line, i.e. ``end_lineno - body[0].lineno + 1``). A body longer
than ``MAX_BODY_LINES`` (120) must justify itself with a ``LONG-TOOL:`` marker
plus a justification sentence in the **contiguous comment block directly above
the function's first decorator** — otherwise this script exits 1 with a report.

Why a comment and not the docstring: a tool function's docstring IS its wire
description, and the justification is internal tooling noise that must not
reach the model-visible surface (audit W13). A comment adjacent to the
decorator records the exemption without changing the tool surface.

Why the body and not the whole ``def``: the gate targets implementation bulk
(the audit's W19 "tools are not thin wrappers"), and the docstring/signature
are not implementation. The chosen metric is stated here so the exemption set
is reproducible — run this script to see the exact over-limit list.

Usage::

    python scripts/check_tool_function_length.py [TOOLS_DIR]

``TOOLS_DIR`` defaults to ``src/ppsspp_dfx_mcp/tools`` (resolved relative to the
repository root, i.e. this file's parent's parent). Exit code 0 = clean.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

MAX_BODY_LINES = 120

#: Marker shape: the literal token followed by at least one non-space char
#: (the justification sentence). "LONG-TOOL: because ..." matches; a bare
#: "LONG-TOOL:" does not.
_MARKER_RE = re.compile(r"LONG-TOOL:\s*\S")

_DEFAULT_TOOLS_DIR = Path(__file__).resolve().parents[1] / "src" / "ppsspp_dfx_mcp" / "tools"


def _is_mcp_tool_decorator(node: ast.expr) -> bool:
    """True for ``@mcp.tool`` and ``@mcp.tool(...)``."""
    target = node.func if isinstance(node, ast.Call) else node
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "tool"
        and isinstance(target.value, ast.Name)
        and target.value.id == "mcp"
    )


def _body_length(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Body span in physical lines: first body statement .. last line."""
    return fn.end_lineno - fn.body[0].lineno + 1 if fn.body else 0


def _contiguous_comment_block(lines: list[str], first_decorator_lineno: int) -> str:
    """Comment lines directly above `first_decorator_lineno` (1-based).

    Walks upward from the line above the first decorator while each line is a
    comment (``#`` after optional indentation); stops at the first blank or
    code line. Returns the collected lines in source order.
    """
    idx = first_decorator_lineno - 2  # 0-based index of the line above the decorator
    collected: list[str] = []
    while idx >= 0 and lines[idx].lstrip().startswith("#"):
        collected.append(lines[idx])
        idx -= 1
    return "\n".join(reversed(collected))


def _check_file(path: Path) -> list[str]:
    """Return violation strings for one module ([] when clean).

    A module that will not parse is itself a violation (the gate must not
    silently skip an unreadable tree).
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno} cannot parse module ({exc.msg})"]
    lines = source.splitlines()
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not any(_is_mcp_tool_decorator(d) for d in node.decorator_list):
            continue
        body = _body_length(node)
        if body <= MAX_BODY_LINES:
            continue
        block = _contiguous_comment_block(lines, node.decorator_list[0].lineno)
        if not _MARKER_RE.search(block):
            violations.append(
                f"{path}:{node.lineno} {node.name}() body={body} lines "
                f"(> {MAX_BODY_LINES}) is missing a 'LONG-TOOL: <justification>' "
                f"comment directly above its @mcp.tool decorator"
            )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tools_dir",
        nargs="?",
        type=Path,
        default=_DEFAULT_TOOLS_DIR,
        help="Directory of tool modules to scan (default: src/ppsspp_dfx_mcp/tools).",
    )
    args = parser.parse_args(argv)

    tools_dir: Path = args.tools_dir
    if not tools_dir.is_dir():
        print(f"ERROR: tools dir not found: {tools_dir}", file=sys.stderr)
        return 2

    violations: list[str] = []
    scanned = 0
    for path in sorted(tools_dir.glob("*.py")):
        if path.name.startswith("_"):
            continue
        scanned += 1
        violations.extend(_check_file(path))

    if violations:
        print(
            f"A19 tool-function length gate FAILED — {len(violations)} over-limit "
            f"function(s) without a 'LONG-TOOL:' justification comment:",
            file=sys.stderr,
        )
        for line in violations:
            print(f"  {line}", file=sys.stderr)
        print(
            f"\nLimit: function body > {MAX_BODY_LINES} lines must carry a "
            f"'LONG-TOOL: <reason>' comment directly above its @mcp.tool decorator. "
            f"Shrink the body, or document why it must stay long.",
            file=sys.stderr,
        )
        return 1

    print(
        f"A19 tool-function length gate OK ({scanned} modules scanned, all bodies <= {MAX_BODY_LINES} lines or justified)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
