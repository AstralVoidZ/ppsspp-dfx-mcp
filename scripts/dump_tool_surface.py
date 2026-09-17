"""Dump the registered MCP tool surface (P6 baseline tooling, H2).

Exports every registered tool's name, description, annotation flags,
description size and inputSchema size — the authoritative "before/after"
meter for the P6 description-slimming effort and the source of truth for
the committed tools/list baseline (tests/unit/l2_mcp_contract/
tool_surface_baseline.json).

Usage (run from mcps/ppsspp-dfx-mcp):
    PYTHONPATH=src python scripts/dump_tool_surface.py \\
        [--out tests/unit/l2_mcp_contract/tool_surface_baseline.json]

The baseline test fails when the live surface drifts from the committed
JSON — regenerate it with this script and review the diff in the same
commit as the description change (never as a drive-by).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from ppsspp_dfx_mcp import server as server_mod

DEFAULT_OUT = (
    Path(__file__).resolve().parents[1]
    / "tests"
    / "unit"
    / "l2_mcp_contract"
    / "tool_surface_baseline.json"
)


def _annotations_dict(ann) -> dict:
    if ann is None:
        return {}
    return {
        "read_only_hint": getattr(ann, "read_only_hint", None),
        "destructive_hint": getattr(ann, "destructive_hint", None),
        "idempotent_hint": getattr(ann, "idempotent_hint", None),
        "open_world_hint": getattr(ann, "open_world_hint", None),
    }


def _canonical(schema: dict | None) -> str:
    """Stable JSON for hashing a schema (sorted keys, no whitespace variance)."""
    return json.dumps(schema or {}, ensure_ascii=False, sort_keys=True)


async def _dump() -> dict:
    server_mod.register_all_tools()
    tools = await server_mod.mcp.list_tools()
    entries = {}
    total_desc_chars = 0
    total_schema_chars = 0
    for t in tools:
        desc = t.description or ""
        # `mcp.list_tools()` returns the WIRE-layer `mcp.types.Tool`, which
        # spells these `input_schema` / `output_schema` (snake_case). The
        # earlier `getattr(t, "inputSchema", None) or {}` used the wire
        # *JSON* spelling, which does not exist on the model — so every
        # tool silently recorded `{}` (all 40 sha1s identical) and the
        # baseline could not detect a parameter change at all. Access the
        # attribute directly: a missing one must crash, not degrade.
        input_schema = t.input_schema
        output_schema = t.output_schema
        schema_json = _canonical(input_schema)
        output_json = _canonical(output_schema)
        words = len(desc.split())
        total_desc_chars += len(desc)
        total_schema_chars += len(schema_json)
        entries[t.name] = {
            "description": desc,
            "description_words": words,
            "description_chars": len(desc),
            "input_schema_chars": len(schema_json),
            "input_schema_sha1": hashlib.sha1(schema_json.encode("utf-8")).hexdigest(),
            # Parameter NAMES as a sorted list, not just a hash: when a
            # rename does happen, the baseline diff shows the name instead
            # of two opaque digests.
            "input_schema_properties": sorted((input_schema or {}).get("properties") or {}),
            "output_schema_chars": len(output_json),
            "output_schema_sha1": hashlib.sha1(output_json.encode("utf-8")).hexdigest(),
            "annotations": _annotations_dict(getattr(t, "annotations", None)),
        }
    return {
        "note": (
            "Committed tools/list baseline — regenerate with "
            "scripts/dump_tool_surface.py and review the diff alongside "
            "the description change that caused it."
        ),
        "tool_count": len(entries),
        "total_description_chars": total_desc_chars,
        "total_input_schema_chars": total_schema_chars,
        "tools": dict(sorted(entries.items())),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    dump = asyncio.run(_dump())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(dump, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    print(
        f"tools={dump['tool_count']} "
        f"desc_chars={dump['total_description_chars']} "
        f"schema_chars={dump['total_input_schema_chars']}"
    )
    print(f"written: {args.out}")


if __name__ == "__main__":
    main()
