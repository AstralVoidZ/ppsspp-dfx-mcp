"""Business exception code registry, **derived** from ppsspp_dfx_mcp.errors.

Business exception classes live in ppsspp_dfx_mcp.errors; this module exposes
their ``code`` strings as a lookup table for tests / docs / agent triage.

The registry used to be a hand-maintained tuple of classes (in
``core/error_codes.py``, then an orphaned module). That is a second definition
of the same fact — it claimed 1:1 parity with errors.py while nothing enforced
the claim, so a class added to errors.py would silently never appear here. It
is now *derived* by walking ``ToolError.__subclasses__()``, which makes
"registry == errors.py business exceptions" structurally true; the guard test
``tests/unit/l2_mcp_contract/test_error_code_registry_consistency.py`` pins
both directions (no omission, no extra), code uniqueness, and that every
registered code has a **producer site** in ``src/`` (a ``raise <Class>(...)``
or a ``return <Class>(...)`` — the translation layer emits typed errors by
returning them, not raising them).

The module lives in ``spec/`` (contract facts) rather than ``core/``
(implementation machinery): it is a derived contract surface consumed by
tests/docs, not low-level SDK code.
"""

from __future__ import annotations

from ppsspp_dfx_mcp.errors import ToolError


def business_exceptions() -> tuple[type[ToolError], ...]:
    """Depth-first walk of ``ToolError`` subclasses that declare a str ``code``.

    ToolError itself is skipped (its ``code`` is the generic ``INTERNAL``
    fallback, not a business category), and non-ToolError exceptions raised
    by this codebase (e.g. ``SteppingFailedError``, a plain RuntimeError that
    ``to_tool_error`` translates) are outside the tree by construction.
    Sorted by code so the tuple is stable across definition-order edits.
    """
    found: list[type[ToolError]] = []
    stack: list[type[ToolError]] = list(ToolError.__subclasses__())
    while stack:
        cls = stack.pop()
        if isinstance(getattr(cls, "code", None), str):
            found.append(cls)
        stack.extend(cls.__subclasses__())
    return tuple(sorted(found, key=lambda c: c.code))


# Error code → exception class lookup.
ERROR_CODE_TO_CLASS: dict[str, type[ToolError]] = {cls.code: cls for cls in business_exceptions()}

# Codes that are part of the public error contract (declared in errors.py and
# documented in SKILL / error-codes.md) but have **no producer site** in
# ``src/``. Each entry is a deliberate exemption from the registry test's
# "every code has a raise/return site" rule — justified, not accidental:
#
# - ``PPSSPP_ERROR``: the ``PpssppError`` *abstract base* of the Ppsspp*
#   family; concrete members (PPSSPP_NOT_FOUND / ISO_NOT_FOUND /
#   WS_CONNECT_FAILED) are what callers raise. Never instantiated directly.
#
# Rule for the future (kept explicit so an empty set can't be mistaken for
# "no rule"): a registered code must either have a producer site in ``src/``
# (a ``raise``/``return`` of its class) or be listed here with a reason.
# Adding a new producer-less code without listing it fails
# ``test_every_registered_code_has_a_producer_site``.
RESERVED_CODES: frozenset[str] = frozenset(
    {
        "PPSSPP_ERROR",
    }
)

__all__ = [
    "ERROR_CODE_TO_CLASS",
    "RESERVED_CODES",
    "business_exceptions",
]
