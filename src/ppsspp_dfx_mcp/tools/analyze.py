"""Analyze tool wrappers.

1 tool exposed:
- ppsspp_analyze_log(log_path?, filter?) — filter PPSSPP log for error/warning lines

Pure Python (no WS interaction): analyze_log reads from disk and can run
without a session.
"""

from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.config import config_dir, output_dir
from ppsspp_dfx_mcp.errors import ArgsInvalid, ConfigInvalid
from ppsspp_dfx_mcp.models.analyze import AnalyzeLogResult, LogMatch
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import MAX_LOG_BYTES, MAX_LOG_MATCHES, translate_tool_errors
from ppsspp_dfx_mcp.views.analyze import AnalyzeLogResponse

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    AnalyzeLogOutput = dict[str, Any]
else:
    AnalyzeLogOutput = derive_output_contract("AnalyzeLogOutput", AnalyzeLogResponse)

logger = logging.getLogger(__name__)

__all__ = ["analyze_log"]

# Keywords that mark "interesting" log lines (case-sensitive; PPSSPP log
# convention is uppercase severity prefixes).
_DEFAULT_KEYWORDS: tuple[str, ...] = ("ERROR", "WARNING", "CRASH")


# Whitelist roots: the server-managed .ppsspp-dfx tree (output/ reports
# and reports-adjacent logs, config/ and config-adjacent logs). Everything
# outside is rejected — analyze_log used to read ANY path the caller named.
#
# Computed LAZILY (first use, cached) instead of at module import: the
# module used to call output_dir()/config_dir() at import time, so a bad
# PPSSPP_DFX_PROJECT_ROOT made `register_all_tools()`'s import blow up with
# a bare traceback before startup validation could report it. Resolution
# failures now surface as ConfigInvalid from the call that needs the roots.
@lru_cache(maxsize=1)
def _log_allowed_roots() -> tuple[Path, ...]:
    """The whitelist roots, resolved on first use and cached.

    Raises:
        ConfigInvalid: the configured project root could not be resolved.
    """
    try:
        return (output_dir().resolve(), config_dir().parent.resolve())
    except ConfigInvalid:
        raise
    except Exception as exc:  # noqa: BLE001 — re-raised as an actionable config error
        raise ConfigInvalid(
            f"cannot resolve analyze_log's allowed roots: {exc} — check "
            f"PPSSPP_DFX_PROJECT_ROOT / PPSSPP_DFX_CONFIG_DIR"
        ) from exc


def _resolve_log_path(log_path: str) -> Path:
    """Whitelist-resolve the analyze_log input path.

    Raises ToolError when the path escapes the allowed roots — a prompt
    injection (or a confused caller) must not be able to exfiltrate
    arbitrary files through the match list.
    """
    roots = _log_allowed_roots()
    resolved = Path(log_path).expanduser().resolve()
    if not any(resolved.is_relative_to(root) for root in roots):
        # No server-derived paths here (review-v4 W-6): the allowed roots
        # and the resolved location reveal the server filesystem layout.
        # The whitelist shape is documented in the tool description; the
        # caller's own input may be echoed back (F-1 contract).
        raise ArgsInvalid(
            "log_path is outside the allowed .ppsspp-dfx tree — pass a path "
            "under the server-managed .ppsspp-dfx directory (see tool "
            f"description); got: {log_path!r}"
        )
    return resolved


def _filter_log_lines(
    path: Path, keywords: list[str], required: str | None = None
) -> tuple[list[LogMatch], bool]:
    """Stream-filter a log file for keyword lines (worker-thread target).

    Hard caps: files larger than MAX_LOG_BYTES are rejected up front;
    matching stops at MAX_LOG_MATCHES. Line numbers are 1-based and match
    the previous splitlines() behavior.

    Returns:
        ``(matches, hit_cap)``. ``hit_cap`` is True when MAX_LOG_MATCHES
        stopped the scan, i.e. ``matches`` is a prefix of the real match
        set (the caller used to compare ``len(matches)``
        against ``limit`` only, so a capped scan reported truncated=false
        and total_matches as if it were exact).
    """
    size = path.stat().st_size
    if size > MAX_LOG_BYTES:
        raise ArgsInvalid(f"log file too large: {path} ({size} bytes; cap {MAX_LOG_BYTES})")
    matches: list[LogMatch] = []
    hit_cap = False
    with path.open("r", encoding="utf-8", errors="replace") as f:
        # A-9 (review v4): the pre-open stat() is advisory only — the file
        # can grow between stat and read (stat-then-open TOCTOU). Re-check
        # the cap on the OPEN handle, which is what this loop can read.
        open_size = os.fstat(f.fileno()).st_size
        if open_size > MAX_LOG_BYTES:
            raise ArgsInvalid(
                f"log file too large: {path} ({open_size} bytes; cap {MAX_LOG_BYTES})"
            )
        for i, line in enumerate(f, 1):
            # 'all' narrowing in-stream — the 500-cap then applies to
            # POST-narrow matches instead of silently dropping later hits.
            if required is not None and required not in line:
                continue
            if any(kw in line for kw in keywords):
                matches.append(LogMatch(line_no=i, text=line.rstrip("\r\n")))
                if len(matches) >= MAX_LOG_MATCHES:
                    hit_cap = True
                    break
    return matches, hit_cap


@mcp.tool(
    name="ppsspp_analyze_log",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
@translate_tool_errors
async def analyze_log(
    log_path: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Path to the log file to analyze. Whitelist: must be a "
                "file anywhere inside the server-managed .ppsspp-dfx tree "
                "(the whole tree is allowed, wider than output/ or "
                "config/ — verified against the runtime whitelist); "
                "arbitrary filesystem paths are rejected. If None, reads the "
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
                "Additional keyword to filter for. Case-sensitive "
                "substring match; combined with the severity keywords "
                "per filter_mode."
            ),
        ),
    ] = None,
    filter_mode: Annotated[
        Literal["any", "all"],
        Field(
            default="any",
            description=(
                "'any' (default, legacy): a line matches if it contains "
                "a severity keyword OR the filter. 'all': a line must "
                "contain a severity keyword AND the filter — use this "
                "to narrow (e.g. filter='GPU', filter_mode='all' → only "
                "GPU-related ERROR/WARNING/CRASH lines)."
            ),
        ),
    ] = "any",
    limit: Annotated[
        int,
        Field(
            default=0,
            description=(
                "Cap the returned match list (0 = no cap beyond the "
                "hard internal cap of 500). When truncation happens the "
                "response carries total_matches and truncated=true; "
                "total_matches is a LOWER BOUND whenever truncated=true "
                "(the scan stops at 500 matches, so the real count can "
                "be higher). Use this on long logs instead of receiving "
                "200KB+ of matches."
            ),
        ),
    ] = 0,
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

    RETURNS: {log_path, matches: [{line_no, text}...], count, filter, filter_mode, total_matches, truncated}. truncated=true covers both an explicit `limit` cut and the internal 500-match cap; total_matches is a lower bound (>= the value) whenever truncated=true."""
    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_analyze_log",
            "log_path": log_path,
            "filter": filter,
            "filter_mode": filter_mode,
        },
    )

    if limit < 0:
        # Negative limit was silently ignored (= no cap).
        raise ArgsInvalid(f"limit must be >= 0 (got {limit})")
    # 'all' narrows in the stream pass; 'any' keeps legacy additive OR.
    required = filter if (filter and filter_mode == "all") else None
    keywords = list(_DEFAULT_KEYWORDS)
    if filter and filter_mode != "all":
        # Legacy 'any' mode: the filter is ADDITIVE (severity OR filter).
        # Callers who read it as a narrowing condition want filter_mode='all'.
        keywords.append(filter)

    if log_path:
        path = _resolve_log_path(log_path)
        # Stream the file line-by-line in a worker
        # thread with hard byte/match caps — the previous implementation
        # read the entire file into memory synchronously (OOM risk on
        # huge files, event-loop stall, unbounded match list).
        matches, hit_cap = await asyncio.to_thread(_filter_log_lines, path, keywords, required)
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
            matches, hit_cap = await asyncio.to_thread(
                _filter_log_lines, mirror_path, keywords, required
            )
            source = str(mirror_path)
        else:
            matches = []
            hit_cap = False
            source = (
                f"(no mirrored ppsspp log yet at {mirror_path} — the file "
                f"is written while a session runs)"
            )

    total = len(matches)
    # Reaching the internal 500-match cap means `total` is a
    # LOWER BOUND, not the real count — surface it as truncated instead of
    # reporting a complete-looking list.
    truncated = hit_cap or bool(limit > 0 and total > limit)
    if limit > 0 and total > limit:
        matches = matches[:limit]

    result = AnalyzeLogResult(
        log_path=source,
        matches=matches,
        count=len(matches),
        filter=filter or "",
        filter_mode=filter_mode,
        total_matches=total,
        truncated=truncated,
    )
    return AnalyzeLogResponse.from_result(result).model_dump(mode="json")
