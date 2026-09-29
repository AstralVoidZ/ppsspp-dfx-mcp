"""Count claims in the docs must not drift from the authoritative sources.

The docs state three counts (static tools / eval scenario cards / test
suite size). Nothing tied them to reality, so they went stale silently:
the tool count sat at 36 after the 37th tool shipped, and the
scenario-card count at 21 after the real tier grew to 49.

This module binds the *machine-checkable* ones to their single sources of
truth:

- static tool count  -> tool_surface_baseline.json["tool_count"]
- scenario cards      -> evals/scenarios.yaml["scenarios"]

Coverage is an explicit file list, not a glob. The v0.1.6 drift sweep
missed `skills/ppsspp-dfx/references/` because it scanned "the top-level
.md files" by intuition — and that subtree is a primary knowledge source
for agents, so a stale count there teaches agents the wrong tool面.

The test-suite size is deliberately NOT pinned to an exact number. It is
checked as a lower bound only, for two structural reasons:

1. Self-reference. This module is part of the suite it would be counting,
   so any exact figure would be off by the tests added alongside it.
2. Platform variance. CI runs ubuntu/windows/macos x py3.13/3.14, and
   several tests skip conditionally (POSIX-only paths, workspace-only
   fixtures), so the collected count is not one number.

A loose bound still catches the real failure mode — a count left far
behind after the suite grows — without manufacturing false breakage.

Run (from the repository root):
    <venv python> -m pytest tests/unit/l2_mcp_contract/test_readme_claims.py -q
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]
_BASELINE = Path(__file__).resolve().parent / "tool_surface_baseline.json"
_SCENARIOS = _REPO / "evals" / "scenarios.yaml"

# Every file that advertises a count. Keep this list explicit rather than
# globbing: the v0.1.6 drift sweep missed `skills/` precisely because it
# scanned "the top-level .md files" by intuition instead of by an explicit
# list, and the skills references are a primary knowledge source for agents.
_TOOL_COUNT_DOCS = (
    "README.md",
    "README.en.md",
    "skills/ppsspp-dfx/references/architecture.md",
)
_PAIR_DOCS = ("README.md", "README.en.md")


def _baseline_tool_count() -> int:
    assert _BASELINE.is_file(), f"baseline missing: {_BASELINE}"
    return json.loads(_BASELINE.read_text(encoding="utf-8"))["tool_count"]


def _scenario_card_count() -> int:
    assert _SCENARIOS.is_file(), f"scenarios missing: {_SCENARIOS}"
    cfg = yaml.safe_load(_SCENARIOS.read_text(encoding="utf-8"))
    return len(cfg["scenarios"])


def _read(rel: str) -> str:
    path = _REPO / rel
    assert path.is_file(), f"{rel} missing at {path}"
    return path.read_text(encoding="utf-8")


# --- tool count: every "N <tools word>" claim must equal the baseline ----


@pytest.mark.parametrize("doc", _TOOL_COUNT_DOCS)
def test_static_tool_count_matches_baseline(doc: str) -> None:
    """Feature bullet, protocol table, and the skills architecture diagram."""
    text = _read(doc)
    expected = _baseline_tool_count()
    found = re.findall(r"\*\*(\d+) (?:个静态工具|static tools)\*\*", text)
    found += re.findall(r"\|\s*✅\s*\|\s*(\d+) (?:个静态工具|static tools)", text)
    found += re.findall(r"(\d+) (?:个静态工具|static tools)", text)
    assert found, f"{doc}: no static-tool count claim found — wording changed?"
    bad = sorted({n for n in found if int(n) != expected})
    assert not bad, (
        f"{doc}: static tool count {bad} != baseline {expected} "
        f"(tool_surface_baseline.json). Update this doc in the same commit "
        f"as the tool-surface change."
    )


# --- scenario cards: must equal the number of cards in scenarios.yaml ----


@pytest.mark.parametrize("readme", _PAIR_DOCS)
def test_scenario_card_count_matches_yaml(readme: str) -> None:
    text = _read(readme)
    expected = _scenario_card_count()
    found = re.findall(r"(\d+) (?:张场景卡|scenario cards)", text)
    assert found, f"{readme}: no scenario-card count claim found — wording changed?"
    bad = [n for n in found if int(n) != expected]
    assert not bad, (
        f"{readme}: scenario card count {bad} != {expected} cards in "
        f"evals/scenarios.yaml. Update the README in the same commit as the "
        f"card change."
    )


# --- test suite size: lower bound, ast-derived (see module docstring) ----


def _count_test_functions() -> int:
    """Count `def test_*` across the suite, without parametrize expansion.

    Matches the README's stated basis ("excluding parametrize expansion"),
    so the two are comparable. Parsed with ast rather than a regex so a
    `test_` name inside a string or comment cannot inflate the count.
    """
    import ast

    total = 0
    for path in (_REPO / "tests").rglob("test_*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # a file that cannot parse is not a test we can claim
            continue
        total += sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )
    return total


@pytest.mark.parametrize("readme", _PAIR_DOCS)
def test_test_suite_size_is_claimed_as_a_floor(readme: str) -> None:
    """A stale floor is the failure mode; an exact pin would rot by design.

    The docs keep the "N+ test cases" shape, so any exact figure would be
    wrong the moment a test is added (this module included) and would vary
    by platform. Instead the floor is checked against the number of test
    functions actually present: a floor far above reality means the claim
    became a lie as the suite shrank, and a floor far below it means the
    number was simply left behind as the suite grew — the exact drift that
    prompted this guard.

    Tolerance: the floor must not exceed the real count, and must stay
    within 25% of it. Empirically (1350 test functions): 1200+/1300+/1350+
    all pass, while 1400+ (overstated) and 100+ (left far behind) fail —
    both directions of drift are caught.
    """
    text = _read(readme)
    m = re.search(r"(\d+)\+ (?:个测试用例|test cases)", text)
    assert m, (
        f"{readme}: no 'N+ test cases' claim found — the suite size is "
        f"advertised as a floor; keep that shape."
    )
    claimed = int(m.group(1))
    actual = _count_test_functions()
    assert claimed <= actual, (
        f"{readme}: claims {claimed}+ test cases but only {actual} test "
        f"functions exist — the advertised floor overstates the suite."
    )
    assert claimed >= actual * 0.75, (
        f"{readme}: claims {claimed}+ test cases while {actual} exist — the "
        f"floor was left behind as the suite grew (raise it, keeping the "
        f"'N+' shape)."
    )
