"""Analyze tool wrappers.

2 tools exposed:
- ppsspp_analyze_log(log_path?, filter?) — filter PPSSPP log for error/warning lines
- ppsspp_convert_address(address, mode?) — convert IDA ↔ PPSSPP addresses

Pure Python (no WS interaction): analyze_log reads from disk;
convert_address reads config.addresses(). Both can run without a session.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from mcp.types import ToolAnnotations
from typing import Annotated, Any, Literal

from pydantic import Field

from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.config import addresses as _addresses
from ppsspp_dfx_mcp.config import config_dir, output_dir
from ppsspp_dfx_mcp.errors import AddrInvalid, ToolError
from ppsspp_dfx_mcp.models.analyze import (
    AddressConversionResult,
    AnalyzeLogResult,
    LogMatch,
)
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.tools._common import MAX_LOG_BYTES, MAX_LOG_MATCHES
from ppsspp_dfx_mcp.views.analyze import (
    AddressConversionResponse,
    AnalyzeLogResponse,
)

from ppsspp_dfx_mcp.views._contract import derive_output_contract

AnalyzeLogOutput = derive_output_contract("AnalyzeLogOutput", AnalyzeLogResponse)
AddressConversionOutput = derive_output_contract("AddressConversionOutput", AddressConversionResponse)

logger = logging.getLogger(__name__)

__all__ = ["analyze_log", "convert_address"]

# Keywords that mark "interesting" log lines (case-sensitive; PPSSPP log
# convention is uppercase severity prefixes).
_DEFAULT_KEYWORDS: tuple[str, ...] = ("ERROR", "WARNING", "CRASH")

# S3 whitelist roots: the server-managed .ppsspp-dfx tree (output/ reports
# and reports-adjacent logs, config/ and config-adjacent logs). Everything
# outside is rejected — analyze_log used to read ANY path the caller named.
_LOG_ALLOWED_ROOTS: tuple[Path, ...] = (
    output_dir().resolve(),
    config_dir().parent.resolve(),
)


def _coerce_yaml_int(value: Any) -> int:
    """Coerce an addresses.yaml scalar to int, accepting the quoted
    hex-string form ("0x08804000"). Mirrors client_helper.read_game_mode_addr.
    """
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        try:
            return int(value, 16) if value.lower().startswith("0x") else int(value)
        except ValueError:
            pass
    raise ToolError(
        f"addresses.yaml value is not a valid int: {value!r}",
        code="CONFIG_INVALID",
    )


def _resolve_log_path(log_path: str) -> Path:
    """Whitelist-resolve the analyze_log input path (S3 fix).

    Raises ToolError when the path escapes the allowed roots — a prompt
    injection (or a confused caller) must not be able to exfiltrate
    arbitrary files through the match list.
    """
    resolved = Path(log_path).expanduser().resolve()
    if not any(resolved.is_relative_to(root) for root in _LOG_ALLOWED_ROOTS):
        raise ToolError(
            f"log_path is outside the allowed .ppsspp-dfx tree "
            f"(allowed roots: {[str(r) for r in _LOG_ALLOWED_ROOTS]}): "
            f"{resolved}",
            code="INTERNAL",
        )
    return resolved


def _filter_log_lines(path: Path, keywords: list[str]) -> list[LogMatch]:
    """Stream-filter a log file for keyword lines (worker-thread target).

    Hard caps: files larger than MAX_LOG_BYTES are rejected up front;
    matching stops at MAX_LOG_MATCHES. Line numbers are 1-based and match
    the previous splitlines() behavior.
    """
    size = path.stat().st_size
    if size > MAX_LOG_BYTES:
        raise ToolError(
            f"log file too large: {path} ({size} bytes; cap {MAX_LOG_BYTES})",
            code="INTERNAL",
        )
    matches: list[LogMatch] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if any(kw in line for kw in keywords):
                matches.append(LogMatch(line_no=i, text=line.rstrip("\r\n")))
                if len(matches) >= MAX_LOG_MATCHES:
                    break
    return matches


# Former docstring (kept as comment; description is now the TDQS docstring):
# Analyze a PPSSPP log file for error/warning/crash lines.
#
# Returns:
# AnalyzeLogResponse dict: log_path + matches + count + filter.
#
# Raises:
# ToolError: if log_path is provided but unreadable.
@mcp.tool(
    name="ppsspp_analyze_log",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
@translate_tool_errors
async def analyze_log(
    log_path: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Path to the log file to analyze. S3 whitelist: must be a "
                "file under the server-managed .ppsspp-dfx tree "
                "(.ppsspp-dfx/output/ or .ppsspp-dfx/config/); arbitrary "
                "filesystem paths are rejected. If None, reads the "
                "server-mirrored PPSSPP broadcast log "
                "(.ppsspp-dfx/output/ppsspp.log — the running game's own "
                "ERROR/WARNING lines, captured while a session runs)."
            ),
        ),
    ] = None,
    filter: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Additional keyword to filter for (in addition to "
                "ERROR/WARNING/CRASH). Case-sensitive substring match."
            ),
        ),
    ] = None,
    session_id: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional session ID (reserved for future use; ignored).",
        ),
    ] = None,
) -> AnalyzeLogOutput:
    """PURPOSE: Filter a PPSSPP log file for ERROR / WARNING / CRASH lines.

    USAGE: log_path optional (defaults to the server-mirrored PPSSPP broadcast log at .ppsspp-dfx/output/ppsspp.log, written while a session runs); filter optional (keyword); session_id optional.

    BEHAVIOR: READ-ONLY. Reads and filters a log file. Does not contact PPSSPP.

    RETURNS: {log_path, matches: [{line_no, text}...], count, filter}.
    """
    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_analyze_log", "log_path": log_path, "filter": filter},
    )

    keywords = list(_DEFAULT_KEYWORDS)
    if filter:
        keywords.append(filter)

    if log_path:
        path = _resolve_log_path(log_path)
        # Stream the file line-by-line in a worker
        # thread with hard byte/match caps — the previous implementation
        # read the entire file into memory synchronously (OOM risk on
        # huge files, event-loop stall, unbounded match list).
        matches = await asyncio.to_thread(_filter_log_lines, path, keywords)
        source = str(path)
    else:
        # The fallback builds a fresh
        # PpssppLauncher whose log_path was never configured by any
        # construction site — read_log() returned "" unconditionally and
        # the default path could never produce matches (probe-confirmed).
        # The default now reads the server-mirrored PPSSPP broadcast log
        # (attached at startup by configure_logging →
        # attach_ppsspp_log_mirror): the running game's own ERROR/WARNING
        # lines land in output/ppsspp.log as they are broadcast.
        mirror_path = output_dir() / "ppsspp.log"
        if mirror_path.is_file():
            matches = await asyncio.to_thread(
                _filter_log_lines, mirror_path, keywords
            )
            source = str(mirror_path)
        else:
            matches = []
            source = (
                f"(no mirrored ppsspp log yet at {mirror_path} — the file "
                f"is written while a session runs)"
            )

    result = AnalyzeLogResult(
        log_path=source,
        matches=matches,
        count=len(matches),
        filter=filter or "",
    )
    return AnalyzeLogResponse.from_result(result).model_dump(mode="json")


# Former docstring (kept as comment; description is now the TDQS docstring):
# Convert an address between IDA and PPSSPP address spaces.
#
# The PPSSPP runtime base for top.prx is read from addresses.yaml
# (`top_base.ppsspp`, default 0x08804000). The IDA base is
# `top_base.ida` (default 0x00000000).
#
# Returns:
# AddressConversionResponse dict: original + converted + mode + bases.
#
# Raises:
# ToolError (AddrInvalid): on negative address or invalid mode.
@mcp.tool(
    name="ppsspp_convert_address",
    annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
)
@translate_tool_errors
async def convert_address(
    address: Annotated[
        str,
        Field(
            description=(
                "Address to convert, as a hex string (e.g. '0x08804000')."
            ),
        ),
    ],
    mode: Annotated[
        Literal["ida_to_ppsspp", "ppsspp_to_ida", "auto"],
        Field(
            default="auto",
            description=(
                "Conversion mode. 'auto' (default) infers from value: "
                "if address >= top_base.ppsspp, treats as ppsspp_to_ida; "
                "else ida_to_ppsspp."
            ),
        ),
    ] = "auto",
    session_id: Annotated[
        str | None,
        Field(
            default=None,
            description="Optional session ID (reserved for future use; ignored).",
        ),
    ] = None,
) -> AddressConversionOutput:
    """PURPOSE: Convert an address between IDA and PPSSPP address spaces (offset ±0x08804000).

    USAGE: address required; mode optional ('auto' default / 'ida_to_ppsspp' / 'ppsspp_to_ida'); session_id optional.

    BEHAVIOR: READ-ONLY. Pure arithmetic on the address; no PPSSPP contact.

    RETURNS: {original, converted, mode, top_base_ppsspp, top_base_ida}.
    """
    address_int = parse_address(address)
    if address_int < 0:
        raise AddrInvalid(f"address must be >= 0, got {address_int}")

    addrs = _addresses()
    top_base = addrs.get("top_base", {}) if isinstance(addrs, dict) else {}
    if not isinstance(top_base, dict):
        top_base = {}
    # Tolerate quoted YAML values ("0x08804000") — int() alone
    # would raise an untranslated ValueError on the string form.
    ppsspp_base = _coerce_yaml_int(top_base.get("ppsspp", 0x08804000))
    ida_base = _coerce_yaml_int(top_base.get("ida", 0x00000000))
    offset = ppsspp_base - ida_base  # typically 0x08804000

    resolved_mode = mode
    if mode == "auto":
        resolved_mode = "ppsspp_to_ida" if address_int >= ppsspp_base else "ida_to_ppsspp"

    if resolved_mode == "ppsspp_to_ida":
        converted = address_int - offset
        if converted < 0:
            raise AddrInvalid(
                f"ppsspp_to_ida: converted={converted:#x} is negative "
                f"(address={address_int:#x}, offset={offset:#x})"
            )
    elif resolved_mode == "ida_to_ppsspp":
        converted = address_int + offset
    else:
        raise ToolError(
            f"invalid mode={mode!r}; expected ida_to_ppsspp / ppsspp_to_ida / auto",
            code="INTERNAL",
        )

    result = AddressConversionResult(
        original=address_int,
        converted=converted,
        mode=resolved_mode,
        top_base_ppsspp=ppsspp_base,
        top_base_ida=ida_base,
    )
    return AddressConversionResponse.from_result(result).model_dump(mode="json")
