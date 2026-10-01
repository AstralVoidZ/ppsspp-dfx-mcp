"""Business exception code registry, **derived** from ppsspp_dfx_mcp.errors.

Business exception classes live in ppsspp_dfx_mcp.errors; this module exposes
their ``code`` strings as a lookup table for tests / docs / agent triage.

W14 (review v3): the registry used to be a hand-maintained tuple of classes.
That is a second definition of the same fact — it claimed 1:1 parity with
errors.py while nothing enforced the claim, so a class added to errors.py
would silently never appear here (the module was in fact orphaned: no
in-repo consumer, so the drift had no symptom). It is now *derived* by
walking ``ToolError.__subclasses__()``, which makes "registry == errors.py
business exceptions" structurally true; the guard test
``tests/unit/l2_mcp_contract/test_error_code_registry_consistency.py`` pins
both directions (no omission, no extra) and code uniqueness.
"""

from __future__ import annotations

from ppsspp_dfx_mcp.errors import (
    AddrInvalid,
    BreakpointError,
    IrEncodingDetected,
    IsoNotFound,
    ManifestError,
    NotImplemented,
    PpssppError,
    PpssppNotFound,
    RateLimitExceeded,
    ScanNoMatch,
    ScriptContractError,
    ScriptNotFound,
    SessionExpired,
    SessionNotFound,
    StepNoAdvanceError,
    ToolError,
    VerifyMismatch,
    WsConnectFailed,
)


def _collect_business_exceptions() -> tuple[type[ToolError], ...]:
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


# All business exception classes (for documentation / iteration).
BUSINESS_EXCEPTIONS = _collect_business_exceptions()

# Error code → exception class lookup.
ERROR_CODE_TO_CLASS = {cls.code: cls for cls in BUSINESS_EXCEPTIONS}

__all__ = [
    "BUSINESS_EXCEPTIONS",
    "ERROR_CODE_TO_CLASS",
    "ToolError",
    "PpssppError",
    "PpssppNotFound",
    "IsoNotFound",
    "WsConnectFailed",
    "SessionNotFound",
    "SessionExpired",
    "ScriptNotFound",
    "ScriptContractError",
    "ManifestError",
    "AddrInvalid",
    "ScanNoMatch",
    "NotImplemented",
    "StepNoAdvanceError",
    "IrEncodingDetected",
    "VerifyMismatch",
    "BreakpointError",
    "RateLimitExceeded",
]
