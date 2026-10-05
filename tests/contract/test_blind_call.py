"""Blind-call contract: a caller must be able to use a tool from its
description ALONE.

The premise of this whole feature is that an AI agent reads tool
descriptions and nothing else. So "the description is adequate" has to be a
testable property, not an opinion -- otherwise the same defects come back
every time a parameter is renamed.

Each check below states one falsifiable condition about a description and
names the evidence it failed on. They deliberately do NOT try to judge
whether an answer is "good"; they check that a caller can determine WHAT TO
PASS. A description that leaves a caller guessing fails here even if the
implementation happens to guess right.

Regressions these were written for (all real, all found by blind-call audit
on 2026-10-02):
  * read_memory.action carried a truncated fragment from the ppsspp_scan
    migration, naming four parameters that tool does not have
  * scan.session_id said "ignored for narrow" and "narrow still needs it"
    in the same sentence
  * hold_buttons said send_analog releases buttons; it does not
  * breakpoint.size claimed ADDRESS-only matching with a "verified" label,
    while the implementation matches on the exact address+size pair
  * query.name said "func_add only", denying action='register'
  * read_memory.size stated no bound although the implementation enforces one
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from contract._description_checks import (
    contradicts_within,
    has_orphan_clause,
)


def _tools() -> dict[str, Any]:
    from ppsspp_dfx_mcp import server as srv

    srv.register_all_tools()
    return {t.name: t for t in asyncio.run(srv.mcp.list_tools())}


TOOLS = _tools()


def _props(name: str) -> dict[str, Any]:
    return (TOOLS[name].input_schema or {}).get("properties") or {}


def _pdesc(name: str, field: str) -> str:
    return (_props(name).get(field) or {}).get("description") or ""


def _desc(name: str) -> str:
    return TOOLS[name].description or ""


class TestNoCrossToolResidue:
    """A description must not name parameters the tool does not have.

    The read_memory defect was a half-sentence left behind by the scan
    migration. It pointed at four parameters that exist only on ppsspp_scan,
    so a model reading it would build a call that cannot succeed.
    """

    def test_no_parameter_descriptions_name_foreign_parameters(self) -> None:
        # tokens that belong to specific tools, checked against the owner
        foreign = {
            "pattern": "ppsspp_scan",
            "start_addr": "ppsspp_scan",
            "max_results": "ppsspp_scan",
        }
        offenders: list[str] = []
        for tool in TOOLS:
            for field in _props(tool):
                d = _pdesc(tool, field)
                for token, owner in foreign.items():
                    if re.search(rf"\b{token}\b", d) and token not in _props(tool):
                        offenders.append(f"{tool}.{field} mentions {token!r} (belongs to {owner})")
        assert not offenders, "cross-tool residue: " + "; ".join(offenders)

    def test_read_memory_action_is_complete(self) -> None:
        """Every enum value must appear in the action description."""
        d = _pdesc("ppsspp_read_memory", "action")
        enum = _props("ppsspp_read_memory")["action"].get("enum") or []
        missing = [v for v in enum if v not in d]
        assert not missing, f"action values undocumented: {missing}"

    def test_no_description_ends_mid_sentence(self) -> None:
        """The residue was an unclosed parenthetical -- catch that shape.

        Interval notation is exempt: diff_memory documents its range as
        `[start, start+size)`, where the closing paren is mathematical
        (half-open interval), not a bracket. Counting it as an unmatched
        paren flagged a correct description, so strip those first.
        """
        interval = re.compile(r"\[[^\[\]]*\)")
        for tool in TOOLS:
            for field in _props(tool):
                d = _pdesc(tool, field).rstrip()
                if not d:
                    continue
                scrubbed = interval.sub("[]", d)
                assert scrubbed.count("(") == scrubbed.count(")"), (
                    f"{tool}.{field} has unbalanced parentheses: {d[-70:]!r}"
                )


class TestEnumValuesAreDocumented:
    """An enum a caller cannot enumerate is an enum it will guess at."""

    def test_enum_parameters_mention_their_values_or_delegate(self) -> None:
        offenders: list[str] = []
        for tool in TOOLS:
            for field, spec in _props(tool).items():
                enum = spec.get("enum")
                if not enum:
                    continue
                d = spec.get("description") or ""
                mentions_any = any(str(v) in d for v in enum)
                delegates = "action" in field or "valid values" in d.lower()
                if not mentions_any and not delegates:
                    offenders.append(f"{tool}.{field} enum={enum} undocumented")
        assert not offenders, "; ".join(offenders)


class TestNoSelfContradiction:
    """Two statements about the same fact must not disagree."""

    def test_scan_session_id_is_not_contradictory(self) -> None:
        d = _pdesc("ppsspp_scan", "session_id").lower()
        assert "ignored for" not in d, (
            f"scan.session_id still says 'ignored for...' while also saying "
            f"narrow needs it: {d[:120]!r}"
        )

    def test_hold_buttons_does_not_claim_analog_releases(self) -> None:
        """Measured: hold_buttons sends input.buttons.send, send_analog sends
        input.analog.send -- independent axes. A caller waiting on
        send_analog to release would wait forever."""
        d = _desc("ppsspp_hold_buttons").lower()
        assert "send_analog call" not in d, (
            "hold_buttons still claims send_analog releases the buttons"
        )
        assert "does not release" in d or "not release" in d, (
            "hold_buttons should state explicitly that send_analog does NOT release buttons"
        )

    def test_breakpoint_size_states_the_real_matching_rule(self) -> None:
        """The implementation matches address+size exactly (PPSSPP
        BreakpointSubscriber.cpp); the old text claimed ADDRESS only and
        carried a 'verified' label, which is the worst combination."""
        d = _pdesc("ppsspp_breakpoint", "size")
        assert "ADDRESS only" not in d, (
            "breakpoint.size still claims ADDRESS-only matching, which the "
            "implementation contradicts"
        )
        assert "address+size" in d or "address + size" in d, (
            "breakpoint.size should state the exact address+size matching rule"
        )


class TestParametersNeededByAnActionAreNamed:
    """A parameter an action requires must say it serves that action."""

    def test_query_name_covers_the_register_action(self) -> None:
        d = _pdesc("ppsspp_query", "name")
        assert "register" in d.lower(), (
            "query.name says func_add only, but action='register' needs it too "
            f"(top-level USAGE agrees): {d[:110]!r}"
        )


class TestNumericBoundsAreStated:
    """A default with no stated bound is a guess waiting to happen.

    Scope note: this check is deliberately narrow. Three revisions of the
    matcher produced false positives before it became trustworthy -- it
    missed "Capped at", "0..1", "1/2/4", "Maximum number of", and the
    bound implied by a type word such as "uint32". Enumerating every
    phrasing a description may use is not tractable, and a check that cries
    wolf gets ignored, which is worse than no check at all.

    So it asserts only the unambiguous case that was a real defect: a size
    parameter whose bound the implementation ENFORCES while the description
    omits it. Everything else stays with human review, which is what the
    blind-call audit was for.
    """

    def test_read_memory_size_states_the_enforced_cap(self) -> None:
        from ppsspp_dfx_mcp.tools.memory import MAX_SINGLE_READ_BYTES

        d = _pdesc("ppsspp_read_memory", "size")
        assert str(MAX_SINGLE_READ_BYTES) in d, (
            f"read_memory.size does not state the enforced cap "
            f"({MAX_SINGLE_READ_BYTES} bytes); the implementation rejects "
            f"larger reads with ARGS_INVALID"
        )


class TestDefaultsAndDescriptionsAgree:
    """A description saying 'default X' while the schema says Y is a trap."""

    def test_no_contradictory_default_claims(self) -> None:
        offenders: list[str] = []
        for tool in TOOLS:
            for field, spec in _props(tool).items():
                default = spec.get("default")
                if default is None:
                    continue
                d = spec.get("description") or ""
                m = re.search(r"default(?:s? to)?\s+([A-Za-z0-9_.]+)", d, re.I)
                if not m:
                    continue
                claimed = m.group(1).strip("'\"")
                # compare loosely: only flag clear numeric mismatches
                try:
                    if (
                        claimed.lstrip("-").isdigit()
                        and str(default).lstrip("-").isdigit()
                        and int(claimed) != int(default)
                    ):
                        offenders.append(
                            f"{tool}.{field} says default {claimed}, schema has {default}"
                        )
                except ValueError:
                    continue
        assert not offenders, "; ".join(offenders)


class TestRequiredParametersAreDiscoverable:
    """Everything `required` must be explained somewhere in the description."""

    def test_required_params_are_mentioned_in_the_tool_description(self) -> None:
        offenders: list[str] = []
        for tool in TOOLS:
            schema = TOOLS[tool].input_schema or {}
            for field in schema.get("required") or []:
                if field not in _props(tool):
                    continue
                d = _pdesc(tool, field).lower()
                if not d:
                    offenders.append(f"{tool}.{field} is required but has no description")
        assert not offenders, "; ".join(offenders)

    def test_every_tool_has_a_description(self) -> None:
        empty = [n for n in TOOLS if not (TOOLS[n].description or "").strip()]
        assert not empty, f"tools with no description: {empty}"


class TestDescriptionIsWholeAndSelfConsistent:
    """Falsifiability guard for the whole-text integrity checks.

    The four integrity predicates (`verify_no_orphan_clauses`,
    `verify_no_internal_contradiction`, `verify_ends_complete`,
    `verify_cross_references`) run in the fast L2 unit test
    (`tests/unit/l2_mcp_contract/test_description_integrity.py`). Running them
    a second time here would be two copies of the same predicate -- the failure
    mode this feature exists to remove. What stays here is the guard that those
    predicates are not vacuous.

    Defect A1 (`ppsspp_breakpoint.size` trailing orphan clause) is the reason
    the checks exist: the checks that preceded them -- "does the text end
    mid-sentence" and "is the real matching rule stated" -- BOTH passed it,
    because the offending sentence was grammatically complete and the rule it
    stated was true. Only a seam-level check catches that shape.
    """

    def test_checks_are_falsifiable_not_vacuous(self) -> None:
        """Guard against the checks silently becoming no-ops.

        If `has_orphan_clause` ever stops detecting the exact A1 text, the
        integrity assertions in the L2 unit test would pass on a broken tree.
        AGENTS.md section 7: 断言必须可证伪 -- so the predicate is exercised on
        the real defect text here.
        """
        a1_text = (
            "NOTE: PPSSPP matches a memory watchpoint by the exact "
            "address+size pair (BreakpointSubscriber.cpp). mem_remove "
            "therefore resolves the real size via mem_list first, because "
            "removing with the caller's size alone silently fails when it "
            "differs (e.g. a 16-byte watch removed with the default 4)."
            "for bookkeeping, not for matching."
        )
        assert has_orphan_clause(a1_text), "the orphan-clause check went blind"
        assert contradicts_within(a1_text), "the contradiction check went blind"
