"""Business exception code constants.

Business exception classes live in ppsspp_dfx_mcp.errors.
This module re-exports the code strings for use in tests / docs.
"""

from __future__ import annotations

from ppsspp_dfx_mcp.errors import (
    AddrInvalid,
    ArgsInvalid,
    BootTimeout,
    BreakpointError,
    CaptureEmpty,
    ConfigInvalid,
    CpuFreezeSuspected,
    CpuStateError,
    IrEncodingDetected,
    IsoNotFound,
    ManifestError,
    NotImplemented,
    PortConflict,
    PpssppError,
    PpssppNotFound,
    PpssppProtocolError,
    ProtectedAddress,
    RateLimitExceeded,
    ScanNoMatch,
    ScriptContractError,
    ScriptNotFound,
    SessionAlreadyExists,
    SessionAmbiguous,
    SessionBusy,
    SessionExpired,
    SessionNotFound,
    StepInvalid,
    StepNoAdvanceError,
    StepOutError,
    ToolError,
    VerifyMismatch,
    WsConnectFailed,
    WsDisconnected,
    WsTimeout,
)

# All business exception classes (for documentation / iteration).
# W17 (review v2): the registry now mirrors errors.py's business
# exception set 1:1 — it claims to be the single source for code strings,
# so a class missing here is a class whose [CODE] is invisible to the
# registry's consumers (docs generators, gate tests, agent triage).
BUSINESS_EXCEPTIONS = (
    PpssppError,
    PpssppNotFound,
    IsoNotFound,
    WsConnectFailed,
    ArgsInvalid,
    ConfigInvalid,
    SessionNotFound,
    SessionExpired,
    SessionBusy,
    SessionAlreadyExists,
    SessionAmbiguous,
    BootTimeout,
    ScriptNotFound,
    ScriptContractError,
    ManifestError,
    AddrInvalid,
    CaptureEmpty,
    ProtectedAddress,
    ScanNoMatch,
    StepInvalid,
    StepNoAdvanceError,
    NotImplemented,
    IrEncodingDetected,
    VerifyMismatch,
    BreakpointError,
    RateLimitExceeded,
    CpuStateError,
    PortConflict,
    WsTimeout,
    WsDisconnected,
    CpuFreezeSuspected,
    PpssppProtocolError,
    StepOutError,
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
