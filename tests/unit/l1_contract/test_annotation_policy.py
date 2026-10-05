"""Annotation policy + conditional-required coverage (spec 008 US2).

Two classes of defect are pinned here:

  B1 -- the MCP `destructiveHint` annotation was applied ad hoc. Tools with
        `remove` / `reset` / `clear` actions were `false` while the three
        writers were `true`, and no written rule explained the difference.
        A hint that points the wrong way is worse than no hint: clients use
        it to decide on confirmation prompts.

  A4 -- `address` / `version` / `value` carry sentinel schema defaults
        (`"0x0"`, `0`) but are not in the schema's `required` list. A generic
        JSON-Schema client therefore fills the sentinel instead of failing.
        The schema default cannot be dropped without breaking existing
        callers, so FR-007 requires the description's first sentence to state
        the requirement.

Both checks read their expectations from `tools/_common.py`, which is the
single source of truth -- hard-coding the expectations here would recreate
the very drift this feature removes.
"""

from __future__ import annotations

from typing import Any

import pytest

from ppsspp_dfx_mcp.tools._common import (
    CONDITIONAL_REQUIRED_PARAMS,
    DESTRUCTIVE_HINT_POLICY,
    DESTRUCTIVE_TOOLS,
    NON_DESTRUCTIVE_BY_POLICY,
    REMOVAL_ACTIONS,
)

_HINT_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")


@pytest.fixture(scope="module")
def surface() -> dict[str, Any]:
    """Live tool surface, keyed by name. No emulator, no subprocess."""
    from contract._description_checks import load_surface

    return load_surface()


def _dump(node: Any) -> dict[str, Any]:
    return node.model_dump(by_alias=True)


def _schema(node: Any) -> dict[str, Any]:
    return _dump(node).get("inputSchema") or {}


def _props(node: Any) -> dict[str, Any]:
    return _schema(node).get("properties") or {}


def _param_desc(node: Any, field: str) -> str:
    return (_props(node).get(field) or {}).get("description") or ""


def _first_sentence(text: str) -> str:
    for sep in (". ", ".", "\n"):
        idx = text.find(sep)
        if idx != -1:
            return text[:idx]
    return text


def _declared_actions(node: Any) -> set[str]:
    enum = (_props(node).get("action") or {}).get("enum") or []
    return set(enum)


# ---------------------------------------------------------------------------
# G-1 ... G-5: annotation policy
# ---------------------------------------------------------------------------


class TestHintsAreMachineReadable:
    """G-1: every hint must be an explicit boolean on every tool.

    Everything else here is unverifiable if the hints are absent, and the
    failure is silent: `getattr(tool, "readOnlyHint")` returns a missing
    attribute and a naive check reads that as "no policy", not as "broken
    path". That false positive happened for real during phase 0.
    """

    def test_every_tool_declares_all_four_hints(self, surface: dict[str, Any]) -> None:
        offenders: list[str] = []
        for name, node in surface.items():
            ann = _dump(node).get("annotations") or {}
            for key in _HINT_KEYS:
                if not isinstance(ann.get(key), bool):
                    offenders.append(f"{name}.{key}={ann.get(key)!r}")
        assert offenders == [], "; ".join(offenders)

    def test_snake_case_attribute_access_would_be_a_false_negative(
        self, surface: dict[str, Any]
    ) -> None:
        """Guards the取值路径: the alias dump is the only correct access.

        `Tool` is a pydantic model whose python-side fields are snake_case
        (`read_only_hint`); only the JSON alias is camelCase. A check written
        against `getattr(tool, "readOnlyHint")` silently sees nothing and
        reports "all clean" on a tree that has no hints at all.
        """
        node = next(iter(surface.values()))
        assert getattr(node, "readOnlyHint", None) is None, (
            "camelCase attribute access unexpectedly worked -- if the SDK "
            "changed, revisit contracts/annotation-policy.md section 4"
        )
        ann = node.annotations
        assert ann is not None, "Tool.annotations is None; nothing to dump"
        dumped = ann.model_dump() if hasattr(ann, "model_dump") else dict(ann)
        assert "read_only_hint" in dumped, (
            f"expected snake_case python-side keys, got {sorted(dumped)}; the "
            f"by_alias=True dump path assumption changed"
        )
        assert "readOnlyHint" not in dumped, (
            "the python-side model now exposes camelCase keys; re-check "
            "which access path the guards must use"
        )


class TestDestructivePolicyIsConsistent:
    """G-2 / G-3 / G-4: the live hints must match the written policy."""

    def test_policy_registry_is_non_empty(self) -> None:
        assert DESTRUCTIVE_HINT_POLICY.strip()
        assert DESTRUCTIVE_TOOLS, "no tool is declared destructive -- policy looks unapplied"

    def test_destructive_tools_are_marked_true(self, surface: dict[str, Any]) -> None:
        offenders: list[str] = []
        for name in DESTRUCTIVE_TOOLS:
            assert name in surface, f"{name} is in DESTRUCTIVE_TOOLS but is not registered"
            ann = _dump(surface[name]).get("annotations") or {}
            if ann.get("destructiveHint") is not True:
                offenders.append(f"{name}: destructiveHint={ann.get('destructiveHint')!r}")
        assert offenders == [], "; ".join(offenders)

    def test_policy_overrides_are_marked_false(self, surface: dict[str, Any]) -> None:
        offenders: list[str] = []
        for name in NON_DESTRUCTIVE_BY_POLICY:
            assert name in surface, f"{name} is in NON_DESTRUCTIVE_BY_POLICY but is not registered"
            ann = _dump(surface[name]).get("annotations") or {}
            if ann.get("destructiveHint") is not False:
                offenders.append(f"{name}: destructiveHint={ann.get('destructiveHint')!r}")
        assert offenders == [], "; ".join(offenders)

    def test_tools_with_removal_actions_are_accounted_for(self, surface: dict[str, Any]) -> None:
        """G-3 + G-4: nothing may look destructive without an explanation."""
        accounted = set(DESTRUCTIVE_TOOLS) | set(NON_DESTRUCTIVE_BY_POLICY)
        unexplained: list[str] = []
        for name, node in surface.items():
            actions = _declared_actions(node)
            if actions & REMOVAL_ACTIONS and name not in accounted:
                unexplained.append(f"{name} (actions={sorted(actions & REMOVAL_ACTIONS)})")
        assert unexplained == [], (
            "these tools have removal/reset actions but no entry in "
            "DESTRUCTIVE_TOOLS or NON_DESTRUCTIVE_BY_POLICY: " + "; ".join(unexplained)
        )

    def test_destructive_flag_is_not_contradicted_by_policy(self, surface: dict[str, Any]) -> None:
        """G-2: a tool judged non-destructive must not be marked true."""
        offenders = [
            name
            for name in NON_DESTRUCTIVE_BY_POLICY
            if name in surface
            and (_dump(surface[name]).get("annotations") or {}).get("destructiveHint") is True
        ]
        assert offenders == [], "; ".join(offenders)

    def test_read_only_tools_are_not_marked_destructive(self, surface: dict[str, Any]) -> None:
        offenders = [
            name
            for name, node in surface.items()
            if (_dump(node).get("annotations") or {}).get("readOnlyHint") is True
            and (_dump(node).get("annotations") or {}).get("destructiveHint") is not False
        ]
        assert offenders == [], "; ".join(offenders)


# ---------------------------------------------------------------------------
# A4 / FR-007: conditional-required must be stated in the first sentence
# ---------------------------------------------------------------------------


class TestConditionalRequiredIsStated:
    def test_registry_entries_are_all_present(self, surface: dict[str, Any]) -> None:
        missing: list[str] = []
        for key in CONDITIONAL_REQUIRED_PARAMS:
            tool, _, field = key.partition(".")
            if tool not in surface or field not in _props(surface[tool]):
                missing.append(key)
        assert missing == [], f"registry names parameters that do not exist: {missing}"

    def test_entries_are_not_already_schema_required(self, surface: dict[str, Any]) -> None:
        """A stale entry hides the real problem: if the param became required,
        the description no longer has to carry the burden and the entry lies."""
        stale: list[str] = []
        for key in CONDITIONAL_REQUIRED_PARAMS:
            tool, _, field = key.partition(".")
            if field in (_schema(surface[tool]).get("required") or []):
                stale.append(key)
        assert stale == [], (
            f"{stale} are listed as conditional-required but ARE in the schema "
            f"required list -- remove the stale registry entry"
        )

    def test_entries_really_have_a_masking_default(self, surface: dict[str, Any]) -> None:
        """This is *why* the description has to compensate."""
        offenders: list[str] = []
        for key in CONDITIONAL_REQUIRED_PARAMS:
            tool, _, field = key.partition(".")
            pnode = _props(surface[tool]).get(field) or {}
            if "default" not in pnode or pnode.get("default") is None:
                offenders.append(key)
        assert offenders == [], (
            f"{offenders} have no non-null schema default, so a generic client "
            f"would omit them rather than fill a sentinel -- registry entry is wrong"
        )

    @pytest.mark.parametrize("key", sorted(CONDITIONAL_REQUIRED_PARAMS))
    def test_first_sentence_states_the_requirement(self, surface: dict[str, Any], key: str) -> None:
        tool, _, field = key.partition(".")
        desc = _param_desc(surface[tool], field)
        assert desc, f"{key} has no description at all"
        first = _first_sentence(desc)
        assert "Required" in first, (
            f"{key}: the first sentence must state the requirement (FR-007), got: {first!r}"
        )

    def test_mutation_sample_would_be_caught(self, surface: dict[str, Any]) -> None:
        """Falsifiability check: the assertion above must be able to fail."""
        old_shape = (
            "Absolute runtime address to read, as a hex string (e.g. '0x08804000'). "
            "Required for action='register'; ignored for all other actions."
        )
        assert "Required" not in _first_sentence(old_shape), (
            "the pre-fix wording already satisfied the check -- the check is vacuous"
        )

    def test_size_params_are_not_falsely_flagged(self, surface: dict[str, Any]) -> None:
        """Counter-check: `size` defaults are legitimate, so they must NOT be
        in the registry. A guard that flags them is a false positive."""
        size_entries = [k for k in CONDITIONAL_REQUIRED_PARAMS if k.endswith(".size")]
        assert size_entries == [], (
            f"{size_entries} treat a legitimate default as a masking sentinel"
        )
