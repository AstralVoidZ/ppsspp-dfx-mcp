"""Unit tests for B2 support — pseudo-call gating + skill file reader.

Run (from mcps/ppsspp-dfx-mcp/):
  ../.venv/ppsspp-dfx-mcp/Scripts/python.exe -m pytest evals/test_b2_skill.py -q
"""

from __future__ import annotations

from pathlib import Path

from evals.gates import evaluate
from evals.runner import read_skill_file


# ---------------------------------------------------------------------------
# gates skip pseudo calls
# ---------------------------------------------------------------------------

def test_first_tool_skips_pseudo_prefix():
    sc = {"expected_first_tools": ["ppsspp_get_pc"], "gates": [{"type": "first_tool"}]}
    run = {
        "tool_calls": [
            {"name": "skill_read", "args": {"path": "SKILL.md"}, "pseudo": True},
            {"name": "ppsspp_get_pc", "args": {}},
        ],
    }
    assert evaluate(sc, run, None)["success"] is True


def test_first_tool_pseudo_only_fails():
    sc = {"expected_first_tools": ["ppsspp_get_pc"], "gates": [{"type": "first_tool"}]}
    run = {"tool_calls": [{"name": "skill_read", "args": {}, "pseudo": True}]}
    assert evaluate(sc, run, None)["success"] is False


def test_sequence_ignores_pseudo_between_steps():
    sc = {
        "expected_sequence": ["ppsspp_read_memory", "ppsspp_disassemble"],
        "gates": [{"type": "sequence"}],
    }
    run = {
        "tool_calls": [
            {"name": "ppsspp_read_memory", "args": {}},
            {"name": "skill_read", "args": {}, "pseudo": True},
            {"name": "ppsspp_disassemble", "args": {}},
        ],
    }
    assert evaluate(sc, run, None)["success"] is True


def test_final_call_ok_ignores_trailing_pseudo():
    sc = {"gates": [{"type": "final_call_ok"}]}
    ok = evaluate(sc, {"tool_calls": [
        {"name": "ppsspp_get_pc", "args": {}, "is_error": False},
        {"name": "skill_read", "args": {}, "pseudo": True, "is_error": False},
    ]}, None)
    bad = evaluate(sc, {"tool_calls": [
        {"name": "ppsspp_get_pc", "args": {}, "is_error": True, "error_code": "X"},
        {"name": "skill_read", "args": {}, "pseudo": True},
    ]}, None)
    assert ok["success"] is True
    assert bad["success"] is False


def test_no_tool_counts_pseudo():
    sc = {"gates": [{"type": "no_tool"}]}
    run = {"tool_calls": [{"name": "skill_read", "args": {}, "pseudo": True}]}
    assert evaluate(sc, run, None)["success"] is False


# ---------------------------------------------------------------------------
# read_skill_file
# ---------------------------------------------------------------------------

def test_read_skill_file_ok(tmp_path: Path):
    (tmp_path / "SKILL.md").write_text("# skill", encoding="utf-8")
    sub = tmp_path / "references"
    sub.mkdir()
    (sub / "error-codes.md").write_text("| E |", encoding="utf-8")
    assert read_skill_file("SKILL.md", tmp_path) == "# skill"
    assert read_skill_file("references/error-codes.md", tmp_path) == "| E |"


def test_read_skill_file_missing(tmp_path: Path):
    assert read_skill_file("nope.md", tmp_path).startswith("[skill_read] file not found")


def test_read_skill_file_traversal_guard(tmp_path: Path):
    out = read_skill_file("../../pyproject.toml", tmp_path)
    assert out.startswith("[skill_read] path escapes skill root")
