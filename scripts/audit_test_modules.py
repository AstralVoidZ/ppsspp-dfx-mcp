"""Test-module doctor for ppsspp-dfx-mcp (R-E, 2026-09-08).

Makes the v4 verification report's structural statistics repeatable
(design_ppsspp_dfx_mcp_test_module_refactor_v1 §R-E / success
criterion 2):

- scenario totals per phase + per-tool coverage matrix
- hollow-ok ratio (expect=ok scenarios with no validator — G-2),
  gated at 20% with --check
- zero-coverage tools (no harness scenario at all — G-1)
- MCP-surface presence checks (prompts / resources / dynamic scripts)

Usage (from the repository root):
    PYTHONPATH=src python scripts/audit_test_modules.py [--check]
Exit 1 with --check when any gate trips.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_real_mcp as harness  # noqa: E402
from _wire import PACKAGE_ROOT

BASELINE = PACKAGE_ROOT / "tests" / "unit" / "l2_mcp_contract" / "tool_surface_baseline.json"

HOLLOW_OK_MAX_RATIO = 0.20
# Session-independent tools may legitimately live in phase A only.
A_ONLY_OK = {
    "ppsspp_health",
    "ppsspp_list_addresses",
    "ppsspp_list_scripts",
    "ppsspp_reload_scripts",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--check",
        action="store_true",
        help="exit 1 when a gate trips (hollow-ok ratio, zero-coverage tools, missing MCP surface)",
    )
    args = ap.parse_args()

    all_s = list((*harness.PHASE_A, *harness.PHASE_B))
    by_tool: Counter[str] = Counter(s.tool for s in all_s)
    hollow = [s.id for s in all_s if s.expect == "ok" and s.validator is None]
    ratio = len(hollow) / len(all_s) if all_s else 1.0

    print(f"scenarios: {len(all_s)} (A={len(harness.PHASE_A)}, B={len(harness.PHASE_B)})")
    print(f"hollow-ok: {len(hollow)}/{len(all_s)} = {ratio:.1%} (gate ≤ {HOLLOW_OK_MAX_RATIO:.0%})")
    for sid in hollow:
        print(f"  hollow: {sid}")

    print("\ncoverage matrix (scenario count per tool):")
    for tool, n in sorted(by_tool.items()):
        print(f"  {tool:40s} {n:3d}")

    zero = []
    if BASELINE.exists():
        surface = json.loads(BASELINE.read_text(encoding="utf-8"))["tools"]
        names = (
            sorted(surface.keys())
            if isinstance(surface, dict)
            else sorted(t["name"] for t in surface)
        )
        zero = [t for t in names if by_tool.get(t, 0) == 0]
        print(f"\ntool surface: {len(names)} static tools; zero-coverage: {zero or 'none'}")

    surface_tools = set(by_tool)
    dynamic = sorted(t for t in surface_tools if t.startswith("ppsspp_script_"))
    mcp_surface = {
        "prompts scenarios": by_tool.get("(prompts)", 0) + by_tool.get("(prompt)", 0),
        "resources scenarios": by_tool.get("(resources)", 0) + by_tool.get("(resource)", 0),
        "dynamic script scenarios": sum(by_tool.get(t, 0) for t in dynamic),
    }
    print("\nMCP surface on the wire:")
    for k, v in mcp_surface.items():
        print(f"  {k}: {v}")

    if args.check:
        problems = []
        if ratio > HOLLOW_OK_MAX_RATIO:
            problems.append(f"hollow-ok ratio {ratio:.1%} > {HOLLOW_OK_MAX_RATIO:.0%}")
        real_zero = [t for t in zero if t not in A_ONLY_OK]
        if real_zero:
            problems.append(f"zero-coverage static tools: {real_zero}")
        if mcp_surface["prompts scenarios"] == 0:
            problems.append("no prompts scenarios")
        if mcp_surface["resources scenarios"] == 0:
            problems.append("no resources scenarios")
        if mcp_surface["dynamic script scenarios"] < 2:
            problems.append("dynamic script tools not covered (need ≥2 ppsspp_script_* scenarios)")
        for p in problems:
            print(f"GATE FAIL: {p}")
        raise SystemExit(1 if problems else 0)


if __name__ == "__main__":  # pragma: no cover
    main()
