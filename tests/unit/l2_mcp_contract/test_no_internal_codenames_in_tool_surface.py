"""L2 guard: the public tool surface must carry no internal-review codenames.

The MCP tool surface (each tool's ``description`` plus every ``description``
in its input/output JSON Schema) is shown verbatim to every client model and
committed into ``tool_surface_baseline.json``. Internal review artifacts —
the 🔴/🟡/🟢 severity markers and defect tags like ``D16`` / ``W13`` / ``S3``
/ ``review v2`` — are meaningless to a client and must never leak there.

This walks the REGISTERED tools' schemas rather than grepping source, so it
guards the contract (what a client actually receives), not the text on disk.
A codename that survives in a docstring is harmless; one that reaches a
schema is caught here.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from ppsspp_dfx_mcp import server as server_mod

# Internal-review codename shapes. Kept deliberately tight so ordinary prose
# (hex addresses, "u16", MIPS register names like r5 / f0 / f31) is not
# flagged: all patterns are case-sensitive and require a word boundary.
_CODENAME_PATTERNS = (
    re.compile(r"[🔴🟡🟢]"),
    re.compile(r"review v\d"),
    re.compile(r"review-r\d"),
    re.compile(r"\b[DWS]\d+\b"),
    # Defect-tag families found in the audit's residue sweep (F-5, R2, C2.2,
    # U7, I15, P0-2, V023). `F-?\d` and `R\d` are single-digit-only, so a
    # future "F12" (keyboard) / register prose is not caught by accident.
    re.compile(r"\bF-?\d\b"),
    re.compile(r"\bR\d\b"),
    re.compile(r"\bC\d\.\d\b"),
    re.compile(r"\bU-?\d\b"),
    re.compile(r"\bI\d{2}\b"),
    re.compile(r"\bP\d+-\d+\b"),
    re.compile(r"\bV\d{3}\b"),
)


def _iter_schema_descriptions(node: object):
    """Yield every ``description`` string anywhere in a JSON-Schema tree."""
    if isinstance(node, dict):
        desc = node.get("description")
        if isinstance(desc, str):
            yield desc
        for value in node.values():
            yield from _iter_schema_descriptions(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_schema_descriptions(item)


@pytest.fixture(scope="module")
def surface_descriptions() -> dict[str, list[str]]:
    """Tool name -> every description string exposed on the wire."""
    server_mod.register_all_tools()
    tools = asyncio.run(server_mod.mcp.list_tools())
    out: dict[str, list[str]] = {}
    for tool in tools:
        strings: list[str] = []
        if tool.description:
            strings.append(tool.description)
        strings.extend(_iter_schema_descriptions(tool.input_schema))
        strings.extend(_iter_schema_descriptions(tool.output_schema))
        out[tool.name] = strings
    return out


def test_registered_surface_is_visible(surface_descriptions: dict[str, list[str]]) -> None:
    """Anchor: the fixture must actually see the tool surface.

    Without this, a registration change that made the walk yield nothing
    would turn the codename test below into a vacuously-passing empty loop.
    """
    assert surface_descriptions, "no registered tools found — the walk is broken"
    assert any(surface_descriptions.values()), "no description strings collected"


def test_no_internal_codenames_reach_the_tool_surface(
    surface_descriptions: dict[str, list[str]],
) -> None:
    """FAIL if any internal-review codename appears in a wire-visible string."""
    violations: list[str] = []
    for name, strings in surface_descriptions.items():
        for text in strings:
            for pattern in _CODENAME_PATTERNS:
                match = pattern.search(text)
                if match:
                    violations.append(f"{name}: {match.group(0)!r} in {text[:120]!r}")
    assert not violations, (
        "internal-review codenames leaked into the public tool surface "
        "(tool description / inputSchema / outputSchema):\n  " + "\n  ".join(violations)
    )
