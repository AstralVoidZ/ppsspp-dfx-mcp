"""Count claims in the docs must not drift from the authoritative sources.

The docs state three counts (static tools / eval scenario cards / test
suite size). Nothing tied them to reality, so they went stale silently:
the tool count sat at 36 after the 37th tool shipped, and the
scenario-card count at 21 after the real tier grew to 49.

This module binds the *machine-checkable* ones to their single sources of
truth:

- static tool count  -> tool_surface_baseline.json["tool_count"]
- scenario cards      -> evals/scenarios.yaml["scenarios"]

It also guards a non-numeric drift: retired tool names (v0.1.6 surface
merge) must not survive in the user-facing docs, where they advertise
tools that no longer exist.

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
    "CONTRIBUTING.md",
    "skills/ppsspp-dfx/references/architecture.md",
)
_SCENARIO_DOCS = ("README.md", "README.en.md", "evals/README.md")
_PAIR_DOCS = ("README.md", "README.en.md")

# Scripts that describe the tool surface in docstrings/comments. They are not
# prose docs, so the multi-language regex above does not apply — and they
# drifted unpoliced (verify_real_mcp.py claimed "36 tools", record_fixtures.py
# "30 tools") while the real surface was 37. A numeric tool-count claim here
# must equal the baseline; the preferred state is no number at all (reference
# tool_surface_baseline.json instead).
_SCRIPT_TOOL_COUNT_PATHS = (
    "scripts/verify_real_mcp.py",
    "scripts/record_fixtures.py",
)

# Retired v0.1.6 tool names. They merged into dispatchers / other tools
# (mapping table in CHANGELOG.md); a surviving mention advertises a tool
# that cannot be called, so it is checked by an explicit list rather than
# by every "ppsspp_*" token in prose (renamed/absorbed tools are still
# legitimately named in CHANGELOG history and in code comments).
_RETIRED_NAME_DOCS = (
    "README.md",
    "README.en.md",
    "CONTRIBUTING.md",
    "evals/README.md",
    "docs/SCOPE.md",
)
_RETIRED_TOOL_NAMES = (
    "ppsspp_get_pc",
    "ppsspp_smoke_test",
    "ppsspp_dump_texture",
    "ppsspp_dump_clut",
    "ppsspp_session_list",
    "ppsspp_convert_address",
    "ppsspp_wait_breakpoint",
    "ppsspp_trace_memory_access",
)


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
    # CONTRIBUTING.md states it in prose: "snapshots the *static* registry
    # (36 tools)". Parenthesized so the module-docstring example in that
    # file ("3 tools exposed:") cannot be mistaken for a claim.
    found += re.findall(r"\((\d+) tools\)", text)
    assert found, f"{doc}: no static-tool count claim found — wording changed?"
    bad = sorted({n for n in found if int(n) != expected})
    assert not bad, (
        f"{doc}: static tool count {bad} != baseline {expected} "
        f"(tool_surface_baseline.json). Update this doc in the same commit "
        f"as the tool-surface change."
    )


# --- tool count in scripts: any numeric claim must equal the baseline -----


@pytest.mark.parametrize("script", _SCRIPT_TOOL_COUNT_PATHS)
def test_scripts_do_not_restate_stale_tool_count(script: str) -> None:
    """A numeric "<N> tools" / "<N>-tool" claim in these scripts must be current.

    Regex notes: ``(?<!\\w)`` keeps identifiers like "H1/H2 tools" from being
    read as "2 tools"; the separator is a space or hyphen ("36 tools",
    "30-tool"). The preferred fix is to drop the number and reference
    tool_surface_baseline.json, but a *correct* number is tolerated so the
    guard fails only on real drift — a stale claim (36 / 30 when the baseline
    is 37) makes ``bad`` non-empty.
    """
    text = _read(script)
    expected = _baseline_tool_count()
    found = re.findall(r"(?<!\w)(\d+)[- ]tools?\b", text)
    bad = sorted({n for n in found if int(n) != expected})
    assert not bad, (
        f"{script}: stale tool count {bad} != baseline {expected} "
        f"(tool_surface_baseline.json). Do not restate the count here — "
        f"reference the baseline file instead."
    )


# --- retired tool names: must not survive in the user-facing docs ---------


@pytest.mark.parametrize("doc", _RETIRED_NAME_DOCS)
def test_no_retired_tool_names(doc: str) -> None:
    """v0.1.6 retired the names below; the docs must not advertise them.

    The mapping old name -> current tool lives in CHANGELOG.md (that is the
    history channel); the READMEs / SCOPE / evals README are the *current*
    contract and a renamed base is enough to give an agent a tool that
    cannot be called.
    """
    text = _read(doc)
    hits = sorted(name for name in _RETIRED_TOOL_NAMES if name in text)
    assert not hits, (
        f"{doc}: retired tool name(s) {hits} still referenced. Use the "
        f"current tool (mapping table in CHANGELOG.md, v0.1.6 surface merge)."
    )


# --- scenario cards: must equal the number of cards in scenarios.yaml ----


@pytest.mark.parametrize("readme", _SCENARIO_DOCS)
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


# ============================================================================
# Review-v4 W-9: bilingual README structural symmetry
# ============================================================================

_BILINGUAL_HEADING_MAP = {
    "项目状态": "Project status",
    "功能特性": "Features",
    "运行": "Running",
    "配置": "Configuration",
    "协议面": "Protocol surface",
    "错误处理": "Error handling",
    "性能参考（本机实测）": "Performance reference (measured locally)",
    "社区与支持": "Community & support",
    "贡献": "Contributing",
    "开发": "Development",
    "致谢": "Acknowledgements",
    "引用": "Citation",
    "许可证": "License",
}


def test_readme_language_versions_cover_the_same_sections() -> None:
    """The two README language versions must cover the same `##` sections.

    Numeric claim guards went green while the English version silently lost
    the entire「性能参考 / Performance reference」section — count guards
    cannot see structural drift. A new Chinese heading must be mapped here
    (or the section deliberately dropped from the map with a reason), and
    both files must carry it.
    """
    zh = (_REPO / "README.md").read_text(encoding="utf-8")
    en = (_REPO / "README.en.md").read_text(encoding="utf-8")
    zh_headings = {ln[3:].strip() for ln in zh.splitlines() if ln.startswith("## ")}
    en_headings = {ln[3:].strip() for ln in en.splitlines() if ln.startswith("## ")}
    unmapped = zh_headings - _BILINGUAL_HEADING_MAP.keys()
    assert not unmapped, f"## headings missing from _BILINGUAL_HEADING_MAP: {unmapped}"
    expected_en = {_BILINGUAL_HEADING_MAP[h] for h in zh_headings}
    assert expected_en == en_headings, (
        f"README.md/README.en.md sections diverged: "
        f"missing-in-en={sorted(expected_en - en_headings)}, "
        f"missing-in-zh={sorted(en_headings - expected_en)}"
    )


# --- review-v4 W-10 follow-up: SCOPE.md ships verification commands whose
# --- expected output IS a tool count; pin those to the baseline directly.


def test_scope_md_tool_count_claims_match_baseline() -> None:
    """SCOPE.md's '-> 37)' command outputs and '清单（37）' headings must
    equal the baseline — an unmaintained count there reads as authoritative
    right next to the commands that verify it."""
    text = _read("docs/SCOPE.md")
    claims = re.findall(r"-> (\d+)\)", text)
    claims += re.findall(r"静态工具清单（(\d+)）", text)
    baseline = _baseline_tool_count()
    assert claims, "SCOPE.md no longer states a tool count — update this guard"
    assert {int(c) for c in claims} == {baseline}, claims
