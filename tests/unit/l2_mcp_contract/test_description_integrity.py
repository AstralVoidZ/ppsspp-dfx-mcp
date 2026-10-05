"""Description integrity: a description must be whole and self-consistent.

This is the L2 (MCP contract) side of the same property checked by
`tests/contract/test_blind_call.py`. Splitting them lets the unit-level test
run fast and fail with a precise field name, while the contract test keeps
the cross-tool checks.

Falsifiability requirement (AGENTS.md section 7): every assertion below is
paired with a **mutation sample** -- text that must make the check fail. The
mutation samples are taken verbatim from real defects found on 2026-10-02, so
if a check stops detecting them, the check is broken even though the tree
looks clean.

Defects covered:
  A1  ppsspp_breakpoint.size -- orphaned trailing clause contradicting the
      preceding sentence about address+size matching
  A2  the two pre-existing checks (mid-sentence, real-matching-rule) both
      passed A1; these checks close that hole
  A3  ppsspp_disassemble.count -- the `0` substitution is not documented
"""

from __future__ import annotations

import pytest
from contract._description_checks import (
    contradicts_within,
    ends_mid_sentence,
    has_orphan_clause,
    is_negation_aware,
    param_description,
    verify_cross_references,
    verify_ends_complete,
    verify_no_internal_contradiction,
    verify_no_orphan_clauses,
)

# --- mutation samples (real text; a check that stops failing on these is broken) ---

A1_MUTATION = (
    "Memory breakpoint watch size in bytes (default 4). "
    "NOTE: PPSSPP matches a memory watchpoint by the exact address+size pair. "
    "mem_remove therefore resolves the real size via mem_list first, because "
    "removing with the caller's size alone silently fails when it differs "
    "(e.g. a 16-byte watch removed with the default 4).for bookkeeping, not for matching."
)

MID_SENTENCE_MUTATION = "Read action. Valid values: - 'read_bytes' ("

HOLD_BUTTONS_NEGATION_SAMPLE = (
    "Note: send_analog does NOT release buttons -- it drives the analog axes "
    "on a separate PPSSPP event."
)


class TestChecksAreFalsifiable:
    """Each primitive must fail on its mutation sample (T009 / FR-009)."""

    def test_orphan_clause_check_fails_on_A1_sample(self) -> None:
        assert has_orphan_clause(A1_MUTATION), (
            "R-ORPHAN-CLAUSE stopped detecting the real A1 text; the check is broken"
        )

    def test_orphan_clause_check_accepts_clean_text(self) -> None:
        clean = (
            "Memory breakpoint watch size in bytes (mem_set / mem_remove; default 4). "
            "Fixed-width watches use 1/2/4; larger sizes are passed through as a range watch."
        )
        assert not has_orphan_clause(clean)

    def test_mid_sentence_check_fails_on_truncated_sample(self) -> None:
        assert ends_mid_sentence(MID_SENTENCE_MUTATION)

    def test_mid_sentence_check_accepts_terminated_text(self) -> None:
        assert not ends_mid_sentence("Reads raw bytes (requires address + size).")

    def test_contradiction_check_fails_on_A1_clause(self) -> None:
        assert contradicts_within(A1_MUTATION), (
            "the trailing 'for bookkeeping, not for matching' clause must be flagged"
        )

    def test_negation_awareness_detects_the_hold_buttons_wording(self) -> None:
        # Guards the second round's false positive: a checker that cannot see
        # "does NOT" reads this as the claim that send_analog releases buttons.
        assert is_negation_aware(HOLD_BUTTONS_NEGATION_SAMPLE)


class TestLiveSurfaceIsClean:
    """The registered surface must satisfy all of the above (the real goal)."""

    def test_no_orphan_clauses(self) -> None:
        assert verify_no_orphan_clauses() == []

    def test_no_description_ends_mid_sentence(self) -> None:
        assert verify_ends_complete() == []

    def test_no_internal_contradictions(self) -> None:
        assert verify_no_internal_contradiction() == []

    def test_cross_references_resolve(self) -> None:
        assert verify_cross_references() == []


class TestDefectA1IsClosed:
    """Targeted assertions for the reported defect, with field-level precision."""

    def test_breakpoint_size_has_no_orphan_clause(self) -> None:
        text = param_description("ppsspp_breakpoint", "size")
        assert text, "ppsspp_breakpoint.size lost its description"
        assert not has_orphan_clause(text), (
            "ppsspp_breakpoint.size still ends with the orphaned "
            "'for bookkeeping, not for matching.' clause"
        )
        assert "for bookkeeping, not for matching" not in text, (
            "the contradicting clause is still present verbatim"
        )

    def test_breakpoint_size_still_states_the_real_matching_rule(self) -> None:
        # Guard against "fixing" A1 by deleting the useful half as well.
        text = param_description("ppsspp_breakpoint", "size").lower()
        assert "address" in text and "size" in text, (
            "the description must keep explaining that matching uses address+size"
        )


class TestDefectA3IsClosed:
    """`count=0` must be predictable from the description alone."""

    def test_disassemble_count_documents_zero(self) -> None:
        text = param_description("ppsspp_disassemble", "count")
        assert text, "ppsspp_disassemble.count lost its description"
        lower = text.lower()
        # NOTE: a bare `"0" in text` would be satisfied by "100" -- that is a
        # vacuous assertion. Require an actual statement about zero.
        mentions_zero = (
            "count=0" in lower
            or "count = 0" in lower
            or "zero" in lower
            or " 0 " in lower
            or lower.startswith("0 ")
            or "0 is" in lower
            or "0 means" in lower
        )
        assert mentions_zero, (
            "ppsspp_disassemble.count does not state what 0 means; the "
            "implementation substitutes the default of 10, so a caller cannot "
            "predict the result from the description alone"
        )

    @pytest.mark.parametrize("needle", ["default", "10"])
    def test_disassemble_count_states_the_substitution(self, needle: str) -> None:
        text = param_description("ppsspp_disassemble", "count").lower()
        assert needle in text, (
            f"ppsspp_disassemble.count does not mention {needle!r}; the 0 -> 10 "
            f"substitution must be documented"
        )
