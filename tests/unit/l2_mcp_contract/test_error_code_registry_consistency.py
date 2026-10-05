"""The error-code registry must stay derived from errors.py, and live.

`core/error_codes.py` used to hand-maintain a tuple of business exception
classes that *claimed* 1:1 parity with `errors.py`. Nothing enforced the
claim and the module had no consumer, so an omission (or an extra entry)
had no symptom at all — a class whose `[CODE]` was invisible to docs
generators, gate tests, and agent triage.

The registry now lives in `spec/error_codes.py`, derived by walking
`ToolError.__subclasses__()`. These assertions pin the derivation in both
directions, plus the ways a derived registry can still be wrong: duplicate
codes, an empty walk, and — the W23 addition — a **registered code with no
producer site in `src/`** (a code an agent can never actually receive).

Producer-site rule
------------------
Every registered code must have at least one site in `src/` that either
`raise <ClassName>(...)` **or** `return <ClassName>(...)` it — or the code
must be listed in `RESERVED_CODES`. The `return` half is required because
`errors.to_tool_error` is a *factory*: it emits typed errors (e.g.
`WsDisconnected`, `CpuFreezeSuspected`, `PpssppProtocolError`) by returning
them, not raising them (measured 2026-10-03). Scanning only `ast.Raise`
would misclassify those live codes as producer-less.

Run (from the repository root):
    <venv python> -m pytest tests/unit/l2_mcp_contract/test_error_code_registry_consistency.py -q
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ppsspp_dfx_mcp import errors
from ppsspp_dfx_mcp.instructions import INSTRUCTIONS
from ppsspp_dfx_mcp.spec import error_codes

_SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "ppsspp_dfx_mcp"


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


def _called_name(node: ast.expr) -> str | None:
    """Class identifier of a call expression, e.g. ``WsDisconnected(...)``."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return None


def _producer_sites() -> dict[str, set[str]]:
    """Map class name → files where it is raised or returned as a Call.

    A "producer site" is a ``raise <Class>(...)`` **or** ``return
    <Class>(...)`` statement (see the module docstring for why `return`
    counts). Independent of the registry: it reads `src/` directly.
    """
    sites: dict[str, set[str]] = {}
    for path in _SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name: str | None = None
            if isinstance(node, ast.Raise) and node.exc is not None:
                name = _called_name(node.exc)
            elif isinstance(node, ast.Return) and node.value is not None:
                name = _called_name(node.value)
            if name is not None:
                sites.setdefault(name, set()).add(str(path.relative_to(_SRC_ROOT)))
    return sites


def test_registry_matches_errors_module_both_directions() -> None:
    declared = _declared_business_exceptions()
    registered = set(error_codes.business_exceptions())
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
    codes = [cls.code for cls in error_codes.business_exceptions()]
    dupes = sorted({c for c in codes if codes.count(c) > 1})
    assert not dupes, f"重复的错误码 {dupes}——ERROR_CODE_TO_CLASS 会静默丢掉其中一个类的映射"


def test_code_to_class_is_the_inverse_map() -> None:
    expected = {cls.code: cls for cls in error_codes.business_exceptions()}
    assert expected == error_codes.ERROR_CODE_TO_CLASS


def test_every_registered_code_has_a_producer_site() -> None:
    """W23: every registered code must be reachable from a site in `src/`.

    A code whose class is never raised/returned (and is not in
    `RESERVED_CODES`) is a registry entry an agent can never receive — the
    same class of orphan that let `core/error_codes.py` rot. Fails with the
    offending code(s) and their class name(s).
    """
    sites = _producer_sites()
    orphans = sorted(
        f"{cls.code} ({cls.__name__})"
        for cls in error_codes.business_exceptions()
        if cls.code not in error_codes.RESERVED_CODES and cls.__name__ not in sites
    )
    assert not orphans, (
        f"以下注册码在 src/ 中没有任何 raise/return 站点，也不在 RESERVED_CODES 中：{orphans}\n"
        f"（要么为其添加 raise/return 站点，要么在有正当理由时加入 spec/error_codes.py "
        f"的 RESERVED_CODES）"
    )


def test_reserved_codes_are_registered() -> None:
    """RESERVED_CODES 只能豁免真实存在的注册码——拼错的豁免会掩盖规则。"""
    unknown = sorted(error_codes.RESERVED_CODES - set(error_codes.ERROR_CODE_TO_CLASS))
    assert not unknown, f"RESERVED_CODES 含未注册的码：{unknown}"


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
