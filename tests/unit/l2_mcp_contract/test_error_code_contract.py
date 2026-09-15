"""test_error_code_contract.py — L2 MCP contract: error code translation.

Anchor: errors.py — `ToolError` + 16 business subclasses + `to_tool_error`.

Contract:
- `to_tool_error(ToolError)` preserves the original instance + class-level `code`.
- `to_tool_error(Exception)` wraps in a generic ToolError with code="INTERNAL".
- Each ToolError subclass defines a unique `code` class attribute.
- `ToolError(message, code=...)` explicitly overrides the class attribute.
"""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp.errors import (
    AddrInvalid,
    BreakpointError,
    ConfigInvalid,
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
    SessionAlreadyExists,
    SessionExpired,
    SessionNotFound,
    ToolError,
    VerifyMismatch,
    WsConnectFailed,
    to_tool_error,
)


# ============================================================================
# to_tool_error translation
# ============================================================================


class TestToToolErrorTranslation:
    """`to_tool_error` translates any exception to a ToolError."""

    def test_tool_error_passes_through_unchanged(self):
        """A ToolError subclass instance is returned as-is (preserves code)."""
        original = SessionNotFound("no such session")
        result = to_tool_error(original)
        assert result is original, "to_tool_error must return ToolError as-is"
        assert result.code == "SESSION_NOT_FOUND"

    def test_plain_exception_wrapped_as_internal(self):
        """A non-ToolError exception is wrapped with code=INTERNAL."""
        original = RuntimeError("disk i/o failed")
        result = to_tool_error(original)
        assert isinstance(result, ToolError)
        assert result.code == "INTERNAL"
        assert result.__cause__ is None  # no `from` chain set by to_tool_error
        assert "disk i/o failed" in str(result)

    def test_preserves_subclass_code(self):
        """IsoNotFound.code is preserved through to_tool_error."""
        original = IsoNotFound("/tmp/missing.iso")
        result = to_tool_error(original)
        assert result is original
        assert result.code == "ISO_NOT_FOUND"


# ============================================================================
# ToolError code override
# ============================================================================


class TestToolErrorCodeOverride:
    """`ToolError(message, code=...)` explicitly overrides the class attribute."""

    def test_default_code_from_class_attribute(self):
        """Without explicit code=, ToolError uses the class attribute."""
        err = SessionNotFound("missing")
        assert err.code == "SESSION_NOT_FOUND"

    def test_explicit_code_overrides_class_attribute(self):
        """`code=` kwarg overrides the class-level `code`."""
        err = SessionNotFound("missing", code="CUSTOM_CODE")
        assert err.code == "CUSTOM_CODE"

    def test_base_tool_error_default_is_internal(self):
        """Base ToolError.code defaults to 'INTERNAL'."""
        err = ToolError("something broke")
        assert err.code == "INTERNAL"


# ============================================================================
# Subclass code uniqueness & constancy
# ============================================================================


class TestSubclassCodeContract:
    """Each ToolError subclass defines a unique, stable `code` constant.

    These codes are part of the public MCP contract — clients dispatch
    on `code` to render user-facing error messages. Changing a code
    breaks clients silently.
    """

    # (class, expected_code)
    _EXPECTED_CODES = [
        (ToolError, "INTERNAL"),
        (ConfigInvalid, "CONFIG_INVALID"),
        (PpssppError, "PPSSPP_ERROR"),
        (PpssppNotFound, "PPSSPP_NOT_FOUND"),
        (IsoNotFound, "ISO_NOT_FOUND"),
        (WsConnectFailed, "WS_CONNECT_FAILED"),
        (SessionNotFound, "SESSION_NOT_FOUND"),
        (SessionExpired, "SESSION_EXPIRED"),
        (SessionAlreadyExists, "SESSION_ALREADY_EXISTS"),
        (ScriptNotFound, "SCRIPT_NOT_FOUND"),
        (ScriptContractError, "SCRIPT_CONTRACT_ERROR"),
        (ManifestError, "MANIFEST_ERROR"),
        (AddrInvalid, "ADDR_INVALID"),
        (ScanNoMatch, "SCAN_NO_MATCH"),
        (NotImplemented, "NOT_IMPLEMENTED"),
        (IrEncodingDetected, "IR_ENCODING_DETECTED"),
        (VerifyMismatch, "VERIFY_MISMATCH"),
        (BreakpointError, "BREAKPOINT_ERROR"),
        (RateLimitExceeded, "RATE_LIMIT_EXCEEDED"),
    ]

    @pytest.mark.parametrize("cls,expected_code", _EXPECTED_CODES)
    def test_subclass_code_matches_contract(self, cls, expected_code):
        """Each subclass `code` class attribute matches the public contract."""
        assert cls.code == expected_code, (
            f"{cls.__name__}.code = {cls.code!r}, expected {expected_code!r}"
        )

    def test_all_codes_are_unique(self):
        """No two ToolError subclasses share the same `code`."""
        codes = [cls.code for cls, _ in self._EXPECTED_CODES]
        assert len(codes) == len(set(codes)), (
            f"duplicate codes: {sorted([c for c in codes if codes.count(c) > 1])}"
        )

    def test_all_codes_are_uppercase_strings(self):
        """Every code must be an uppercase string with underscores only."""
        for cls, code in self._EXPECTED_CODES:
            assert isinstance(code, str), (
                f"{cls.__name__}.code must be str, got {type(code).__name__}"
            )
            assert code.isupper() or code == "INTERNAL", (
                f"{cls.__name__}.code = {code!r} must be uppercase"
            )
            assert code.replace("_", "").isalnum(), (
                f"{cls.__name__}.code = {code!r} must be alnum + underscore only"
            )

    def test_subclass_isinstance_of_tool_error(self):
        """Every business subclass must be a subclass of ToolError."""
        for cls, _ in self._EXPECTED_CODES:
            assert issubclass(cls, ToolError), (
                f"{cls.__name__} must subclass ToolError"
            )
