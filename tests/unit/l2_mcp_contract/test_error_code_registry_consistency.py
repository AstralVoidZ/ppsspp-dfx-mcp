"""The error-code registry must stay derived from errors.py.

`core/error_codes.py` used to hand-maintain a tuple of business exception
classes that *claimed* 1:1 parity with `errors.py`. Nothing enforced the
claim and the module had no consumer, so an omission (or an extra entry)
had no symptom at all — a class whose `[CODE]` was invisible to docs
generators, gate tests, and agent triage.

The registry is now derived by walking `ToolError.__subclasses__()`. These
assertions pin the derivation in both directions, plus the two ways a
derived registry can still be wrong: duplicate codes and an empty walk.

Run (from the repository root):
    <venv python> -m pytest tests/unit/l2_mcp_contract/test_error_code_registry_consistency.py -q
"""

from __future__ import annotations

import re

from ppsspp_dfx_mcp import errors
from ppsspp_dfx_mcp.core import error_codes
from ppsspp_dfx_mcp.instructions import INSTRUCTIONS


def _declared_business_exceptions() -> set[type[errors.ToolError]]:
    """ToolError subclasses declaring a str ``code`` (recursive).

    Mirrors the registry's own criterion rather than re-using it: the
    registry is the thing under test, so the expectation has to be built
    independently (same rule, separate implementation).
    """
    found: set[type[errors.ToolError]] = set()
    stack: list[type[errors.ToolError]] = list(errors.ToolError.__subclasses__())
    while stack:
        cls = stack.pop()
        if isinstance(getattr(cls, "code", None), str):
            found.add(cls)
        stack.extend(cls.__subclasses__())
    return found


def test_registry_matches_errors_module_both_directions() -> None:
    declared = _declared_business_exceptions()
    registered = set(error_codes.BUSINESS_EXCEPTIONS)
    assert registered == declared, (
        f"注册表与 errors.py 漂移：\n"
        f"  errors.py 有、注册表缺: {sorted(c.__name__ for c in declared - registered)}\n"
        f"  注册表有、errors.py 无: {sorted(c.__name__ for c in registered - declared)}"
    )


def test_registry_walk_is_not_silently_empty() -> None:
    """Anchor the derivation: a broken walk would pass the parity test vacuously."""
    assert error_codes.ERROR_CODE_TO_CLASS["SESSION_NOT_FOUND"] is errors.SessionNotFound
    assert error_codes.ERROR_CODE_TO_CLASS["CPU_FREEZE_SUSPECTED"] is errors.CpuFreezeSuspected


def test_codes_are_unique() -> None:
    codes = [cls.code for cls in error_codes.BUSINESS_EXCEPTIONS]
    dupes = sorted({c for c in codes if codes.count(c) > 1})
    assert not dupes, f"重复的错误码 {dupes}——ERROR_CODE_TO_CLASS 会静默丢掉其中一个类的映射"


def test_code_to_class_is_the_inverse_map() -> None:
    expected = {cls.code: cls for cls in error_codes.BUSINESS_EXCEPTIONS}
    assert expected == error_codes.ERROR_CODE_TO_CLASS


def test_bracketed_codes_in_instructions_are_registered() -> None:
    """Every `[CODE]` token in the instructions text must be a real code.

    `CODE` itself is excluded: the instructions document the *format* with
    the literal placeholder `Error text starts with "[CODE] "`, which is not
    a code. Everything else bracketed must resolve in the registry — the
    triage text is the agent's only first-step guidance, so a typo there
    sends it looking for an error that cannot occur.
    """
    found = set(re.findall(r"\[([A-Z][A-Z_]{3,})\]", INSTRUCTIONS)) - {"CODE"}
    unknown = sorted(found - set(error_codes.ERROR_CODE_TO_CLASS))
    assert not unknown, f"instructions.py 出现未注册的 [CODE]：{unknown}（注册表来自 errors.py）"
