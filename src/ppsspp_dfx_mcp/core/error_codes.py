"""Business exception code constants.

Business exception classes live in ppsspp_dfx_mcp.errors.
This module re-exports the code strings for use in tests / docs.
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
    SessionAmbiguous,
    SessionExpired,
    SessionNotFound,
    StepNoAdvanceError,
    ToolError,
    VerifyMismatch,
    WsConnectFailed,
)

# All business exception classes (for documentation / iteration).
BUSINESS_EXCEPTIONS = (
    PpssppError,
    PpssppNotFound,
    IsoNotFound,
    WsConnectFailed,
    SessionNotFound,
    SessionExpired,
    SessionAmbiguous,
    ScriptNotFound,
    ScriptContractError,
    ManifestError,
    AddrInvalid,
    ScanNoMatch,
    StepNoAdvanceError,
    NotImplemented,
    IrEncodingDetected,
    VerifyMismatch,
    BreakpointError,
    RateLimitExceeded,
)

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
