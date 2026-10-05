"""Shared description-integrity primitives for the contract tests.

Why a shared module: `tests/contract/test_blind_call.py` and
`tests/unit/l2_mcp_contract/test_description_integrity.py` both need to ask
the same question -- "is this description text self-consistent and
complete?" -- and two copies of that logic would drift apart, which is
exactly the failure mode this feature exists to fix.

Every function here answers a *falsifiable* question about a string or about
the live tool surface. `verify_*` helpers return a list of human-readable
violations; an empty list means clean.

Real defects these were written for (2026-10-02):
  * A1 -- `ppsspp_breakpoint.size` ended with an orphaned clause
    (`...default 4).for bookkeeping, not for matching.`) whose meaning
    contradicted the sentence right before it. The two checks that existed
    at the time both passed it: the sentence is grammatically whole, and the
    rule it states really does exist.
  * A2 -- therefore both existing checks had to be extended, not just kept.
  * A3 -- `ppsspp_disassemble.count` documented its cap but not what `0`
    means, although the implementation substitutes the default.
"""

from __future__ import annotations

import re
from typing import Any

#: A sentence terminator or a closing delimiter that may legitimately end a
#: description (``...use format='bytes'.`` / ``...(default 4)`` / ``...`data`).
OK_TERMINATORS = ".!?`'\")]}>"

#: Characters that make a trailing position obviously mid-sentence.
DANGLING_ENDINGS = "(,;:/+-=*|"

#: The A1 seam: a non-letter/non-space character, a period, then a lowercase
#: word that is followed by whitespace or end-of-text.
#:
#: ``...default 4).for bookkeeping, not for matching.`` is the defect this was
#: written for. Three looser variants were tried first and each produced a false
#: positive on real text, so the constraints are all load-bearing:
#:   * ``\.\s{0,1}[a-z]``      -> matched ``e.g. a 16-byte watch``
#:   * ``\.\s*[a-z]``          -> worse, same cause
#:   * ``[^\sA-Za-z]\.[a-z]``  -> matched the path ``(.ppsspp-dfx/output/...``
#: A check that cries wolf gets switched off, so narrow-and-wrong beats
#: broad-and-noisy here.
_ORPHAN_CLAUSE_RE = re.compile(r"[^\sA-Za-z]\.[a-z]{2,}(?:\s|$)")

#: Negation markers. A check that cannot see these will read
#: "send_analog does NOT release buttons" as the claim that it does.
_NEGATION_RE = re.compile(
    r"\b(?:does\s+not|do\s+not|is\s+not|are\s+not|no\s+longer|never|not)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Text-level primitives
# ---------------------------------------------------------------------------


def has_orphan_clause(text: str) -> bool:
    """True when a sentence was cut mid-way and re-joined (A1's shape).

    Detects a non-letter/non-space character followed by ``.<lowercase>``.
    Deliberately narrow, because a loose version produced false positives on
    ``e.g. a 16-byte watch`` and on hostnames -- and a check that cries wolf
    gets bypassed. See ``_ORPHAN_CLAUSE_RE`` for the exact shape.
    """
    if not text:
        return False
    return _ORPHAN_CLAUSE_RE.search(text) is not None


def ends_mid_sentence(text: str) -> bool:
    """True when the text trails off (dangling delimiter, or no terminator)."""
    stripped = text.strip()
    if not stripped:
        return False
    last = stripped[-1]
    if last in DANGLING_ENDINGS:
        return True
    # a bare word/digit at the very end: no terminator was written
    return last not in OK_TERMINATORS


def is_negation_aware(text: str) -> bool:
    """True when the check can see negation markers in ``text``.

    Used as a *guard*: a keyword check that must not fire on
    "does NOT release" has to prove it saw the "NOT". This exists because the
    second round's mechanical checker reported a false positive here.
    """
    return _NEGATION_RE.search(text) is not None


def contradicts_within(text: str) -> list[str]:
    """Return pairs of clauses that appear to assert opposite things.

    Only the one pattern actually observed is implemented: a sentence
    asserting that something *is* the case, followed by a trailing clause
    asserting it is *not* the case for a different reason ("...matches on the
    exact address+size pair. ... for bookkeeping, not for matching.").

    Kept intentionally small -- a general contradiction detector is not
    buildable, and a noisy one gets bypassed.
    """
    problems: list[str] = []
    lowered = text.lower()
    markers = ("for bookkeeping", "not for matching", "only by address", "ignored for matching")
    for marker in markers:
        idx = lowered.find(marker)
        if idx == -1:
            continue
        head = lowered[:idx]
        if "address+size" in head or "address and size" in head or "exact" in head:
            problems.append(
                f"trailing clause {marker!r} contradicts the preceding claim about address+size matching"
            )
    return problems


# ---------------------------------------------------------------------------
# Surface-level helpers
# ---------------------------------------------------------------------------


def load_surface() -> dict[str, Any]:
    """Live tool surface, keyed by tool name.

    Uses the registered MCP tool objects (no emulator, no subprocess).
    """
    import asyncio

    from ppsspp_dfx_mcp import server as srv

    srv.register_all_tools()
    return {t.name: t for t in asyncio.run(srv.mcp.list_tools())}


def surface() -> dict[str, Any]:
    """Cached accessor so repeated calls in one test session are cheap."""
    global _SURFACE_CACHE
    if _SURFACE_CACHE is None:
        _SURFACE_CACHE = load_surface()
    return _SURFACE_CACHE


_SURFACE_CACHE: dict[str, Any] | None = None


def param_description(tool_name: str, field: str) -> str:
    node = surface()[tool_name]
    props = (node.input_schema or {}).get("properties") or {}
    return (props.get(field) or {}).get("description") or ""


def tool_description(tool_name: str) -> str:
    return surface()[tool_name].description or ""


def verify_no_orphan_clauses() -> list[str]:
    """Every parameter description and tool docstring must be clause-clean."""
    violations: list[str] = []
    for name, node in surface().items():
        doc = node.description or ""
        if has_orphan_clause(doc):
            violations.append(f"{name}: tool description contains an orphaned clause")
        props = (node.input_schema or {}).get("properties") or {}
        for field, pnode in props.items():
            text = (pnode or {}).get("description") or ""
            if has_orphan_clause(text):
                violations.append(f"{name}.{field}: description contains an orphaned clause")
    return violations


def verify_ends_complete() -> list[str]:
    """No description may trail off mid-sentence."""
    violations: list[str] = []
    for name, node in surface().items():
        if ends_mid_sentence(node.description or ""):
            violations.append(f"{name}: tool description ends mid-sentence")
        props = (node.input_schema or {}).get("properties") or {}
        for field, pnode in props.items():
            text = (pnode or {}).get("description") or ""
            if text and ends_mid_sentence(text):
                violations.append(f"{name}.{field}: description ends mid-sentence")
    return violations


def verify_no_internal_contradiction() -> list[str]:
    """No description may assert and then contradict itself."""
    violations: list[str] = []
    for name, node in surface().items():
        for problem in contradicts_within(node.description or ""):
            violations.append(f"{name}: {problem}")
        props = (node.input_schema or {}).get("properties") or {}
        for field, pnode in props.items():
            for problem in contradicts_within((pnode or {}).get("description") or ""):
                violations.append(f"{name}.{field}: {problem}")
    return violations


def verify_cross_references() -> list[str]:
    """Tool docs must not name an action that the referenced tool lacks.

    Scoped to `action='<name>'` mentions of *this* package's tools: the doc
    is searched for `ppsspp_<tool>(action='<value')` patterns and the value
    is checked against that tool's `action` enum.
    """
    violations: list[str] = []
    known = surface()

    # collect each tool's declared action enum, when it has one
    actions: dict[str, set[str]] = {}
    for name, node in known.items():
        props = (node.input_schema or {}).get("properties") or {}
        node_action = (props.get("action") or {}).get("enum")
        if node_action:
            actions[name] = set(node_action)

    ref_re = re.compile(r"(ppsspp_[a-z_]+)\s*\(\s*action\s*=\s*['\"]([a-z_]+)['\"]")
    for name, node in known.items():
        blobs = [node.description or ""]
        props = (node.input_schema or {}).get("properties") or {}
        blobs.extend((p or {}).get("description") or "" for p in props.values())
        for blob in blobs:
            for target, action in ref_re.findall(blob):
                if target not in known or target not in actions:
                    continue
                if action not in actions[target]:
                    violations.append(
                        f"{name} references {target}(action='{action}') "
                        f"but {target} declares {sorted(actions[target])}"
                    )
    return violations
