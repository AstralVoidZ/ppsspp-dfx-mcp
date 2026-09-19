"""Scan tool — three-mode memory scanner (ppsspp_scan).

- mode="pattern": byte-pattern search (migrated from read_memory action=scan;
  delegates to DebugClient.scan_memory's chunked reader).
- mode="value": Cheat-Engine-style value scan with narrowing sessions
  (initial → narrow → list → drop). Pure client-side reads; candidates live
  in a bounded per-server registry.
- mode="strings": charset-aware string harvesting (shift_jis / utf-8 / ascii)
  with a quality filter — the localization-specific workhorse. Decoding
  logic lifted from the ppsspp-dfx skill's decode_text.py.

v0.1.6 批 3（Glama 纵深 P0-2/P0-3）。
"""

from __future__ import annotations

import logging
import re
import struct
import time
import uuid
from typing import Annotated, Any, Literal, TypedDict

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.core.primitives import MAX_SINGLE_READ_BYTES
from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError, to_tool_error
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import (
    resolve_session_id,
    session_client,
    validate_session_alive,
)
from ppsspp_dfx_mcp.tools._common import (
    MAX_SCAN_PATTERN_BYTES,
    MAX_SCAN_RANGE_BYTES,
    MIN_SCAN_CHUNK_BYTES,
    translate_tool_errors,
)
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.scan import ScanResponse

logger = logging.getLogger(__name__)

_ScanResponseOut = derive_output_contract("ScanResponseOut", ScanResponse, partial=True)


class _BackgroundSubmitKeys(TypedDict, total=False):
    """背景提交分支返回裸 dict（不走 ScanResponse）—— 🔴-1 键保全社会 here。"""
    action: str
    batch_id: str
    session_id: str
    estimated_s: float
    hint: str


class ScanOutput(_ScanResponseOut, _BackgroundSubmitKeys, total=False):
    """Union contract: pattern/value/strings + background-submit shapes."""

# ── value-scan session registry ──────────────────────────────────────────
_VALUE_SESSIONS: dict[str, dict[str, Any]] = {}
_MAX_VALUE_SESSIONS = 4
_VALUE_DEFAULT_RANGE = 1 << 20  # 1 MiB initial-scan soft cap
_VALUE_HARD_RANGE = 8 << 20  # 8 MiB foreground hard cap
_VALUE_BG_HARD_RANGE = 32 << 20  # 32 MiB background hard cap (full band + slack)
_VALUE_MAX_HITS = 5000

_WIDTHS = {"u8": (1, "B"), "u16": (2, "H"), "u32": (4, "I")}
_OPS = ("eq", "ne", "lt", "gt")


def _reset_value_sessions_for_tests() -> None:
    """Test isolation hook."""
    _VALUE_SESSIONS.clear()


def _decode_hex_pattern(pattern: str) -> bytes:
    try:
        return bytes.fromhex(pattern.replace(" ", ""))
    except ValueError as e:
        raise ArgsInvalid(
            f"pattern is not a valid hex string: {e} — "
            f"hex bytes like 'AABBCCDD' or pattern_type='ascii'"
        ) from e


_SJIS_RUN = re.compile(
    rb"(?:(?:[\x81-\x9F\xE0-\xFC][\x40-\x7E\x80-\xFC])|[\x20-\x7F\xA1-\xDF]){6,}"
)
_UTF8_RUN = re.compile(
    rb"(?:[\x20-\x7E]|[\xC2-\xDF][\x80-\xBF]|"
    rb"[\xE0-\xEF][\x80-\xBF]{2}|[\xF0-\xF4][\x80-\xBF]{3}){6,}"
)
_ASCII_RUN = re.compile(rb"[\x20-\x7E]{6,}")
_RUNS = {"shift_jis": _SJIS_RUN, "utf8": _UTF8_RUN, "ascii": _ASCII_RUN}
_DECODERS = {
    "shift_jis": lambda b: b.decode("shift_jis"),
    "utf8": lambda b: b.decode("utf-8"),
    "ascii": lambda b: b.decode("ascii"),
}


def _cmp(v: int, op: str, value: int) -> bool:
    """Shared value-scan comparison (eq/ne/lt/gt)."""
    if op == "eq":
        return v == value
    if op == "ne":
        return v != value
    if op == "lt":
        return v < value
    return v > value  # op == "gt"


def _merge_runs(addresses: list[int], size: int) -> list[tuple[int, int, list[int]]]:
    """Group sorted addresses into (run_start, span_bytes, [addresses]) runs.

    Gaps ≤ size fold into one read (candidate offsets are still evaluated
    exactly within the run, so grouping never changes semantics) — this is
    what turns an O(N) narrow pass into O(runs) WS round-trips.
    """
    if not addresses:
        return []
    runs: list[tuple[int, int, list[int]]] = []
    start = end = addresses[0]
    members = [addresses[0]]
    for addr in addresses[1:]:
        if addr - end <= size:
            end = addr
            members.append(addr)
            continue
        runs.append((start, end - start + size, members))
        start = end = addr
        members = [addr]
    runs.append((start, end - start + size, members))
    return runs


async def _read_segments(client: Any, start: int, size: int) -> list[tuple[int, bytes]]:
    """Read [start, start+size) as readable (seg_start, bytes) segments.

    Unreadable chunks are SKIPPED — the ppsspp_scan BEHAVIOR contract.
    Segment pairs (not one flat buffer) keep addresses exact when a hole
    splits the range.
    """
    segs: list[tuple[int, bytes]] = []
    for offset in range(0, size, MAX_SINGLE_READ_BYTES):
        chunk = min(MAX_SINGLE_READ_BYTES, size - offset)
        try:
            raw = await client.read_bytes(address=start + offset, size=chunk)
        except Exception:
            continue
        segs.append((start + offset, bytes(raw)))
    return segs


def _decode_pattern(pattern: str, pattern_type: str) -> bytes:
    if pattern_type == "ascii":
        return pattern.encode("ascii")
    return _decode_hex_pattern(pattern)


@mcp.tool(
    name="ppsspp_scan",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
    ),
)
@translate_tool_errors
async def scan(
    mode: Annotated[
        Literal["pattern", "value", "strings"],
        Field(
            description=(
                "Scan mode:\n"
                "- 'pattern': byte-pattern search (hex/ascii) over a range "
                "— migrated from read_memory(action=scan).\n"
                "- 'value': Cheat-Engine-style value scan with narrowing "
                "sessions (phase: initial → narrow → list → drop; "
                "width u8/u16/u32, op eq/ne/lt/gt).\n"
                "- 'strings': charset-aware string harvesting "
                "(charset shift_jis/utf8/ascii, min_len, quality filter) — "
                "returns [{address, text}]."
            ),
        ),
    ],
    session_id: Annotated[
        str | None,
        Field(
            description=(
                "Active session ID; auto-resolved when exactly one session "
                "is active (required for pattern/value initial/strings; "
                "ignored for narrow/list/drop phases which only re-read "
                "candidate addresses — narrow still needs it)."
            ),
        ),
    ] = None,
    start_addr: Annotated[
        str | None,
        Field(
            description="Range start, inclusive, hex string (same format as `address`).",
        ),
    ] = None,
    end_addr: Annotated[
        str | None,
        Field(
            description="Range end, exclusive, hex string (same format as `address`).",
        ),
    ] = None,
    # pattern mode
    pattern: Annotated[
        str | None,
        Field(
            description=(
                "Pattern to scan for (pattern mode). Interpreted per "
                "pattern_type: 'hex' (default, e.g. 'AABBCCDD') or 'ascii'."
            ),
        ),
    ] = None,
    pattern_type: Annotated[
        Literal["hex", "ascii"],
        Field(
            description="How to interpret `pattern` (pattern mode). 'hex' (default) or 'ascii'.",
        ),
    ] = "hex",
    max_results: Annotated[
        int,
        Field(default=100, description="Maximum number of matches (pattern mode, default 100)."),
    ] = 100,
    chunk_size: Annotated[
        int,
        Field(
            default=4096,
            description=(
                "Bytes per read request during chunked scans (default 4096; "
                "clamped to [64, 65536])."
            ),
        ),
    ] = 4096,
    # value mode
    phase: Annotated[
        Literal["initial", "narrow", "list", "drop"] | None,
        Field(
            description=(
                "Value-scan phase (value mode): 'initial' scans the range "
                "for `value`; 'narrow' re-reads candidates and filters by "
                "`op`+`value` (requires explicit session_id; auto-resolve "
                "not supported for this phase); 'list' returns current "
                "candidates; 'drop' releases the session."
            ),
        ),
    ] = None,
    value: Annotated[
        int | None,
        Field(description="Value to scan/narrow for (value mode)."),
    ] = None,
    width: Annotated[
        Literal["u8", "u16", "u32"],
        Field(description="Value width (value mode; default u16)."),
    ] = "u16",
    op: Annotated[
        Literal["eq", "ne", "lt", "gt"],
        Field(description="Comparison for value scans (initial + narrow; default eq)."),
    ] = "eq",
    scan_handle: Annotated[
        str | None,
        Field(description="Value-scan session handle (narrow/list/drop phases)."),
    ] = None,
    # strings mode
    charset: Annotated[
        Literal["shift_jis", "utf8", "ascii"],
        Field(description="String charset (strings mode; default shift_jis)."),
    ] = "shift_jis",
    min_len: Annotated[
        int,
        Field(default=6, description="Minimum string length (strings mode; default 6)."),
    ] = 6,
    quality: Annotated[
        float,
        Field(
            default=0.2,
            description=(
                "CJK-ratio quality floor for shift_jis (strings mode; "
                "0..1, default 0.2; 0 disables). Random bytes can "
                "chance-decode to kana — the filter keeps signal. "
                "ascii/utf8 have no quality filter — expect noise in "
                "code regions."
            ),
        ),
    ] = 0.2,
    background: Annotated[
        bool,
        Field(
            description=(
                "Run as a detached background job (recommended for "
                "full-band scans): returns a batch_id immediately; poll "
                "ppsspp_batch_status(batch_id=...), cancel via "
                "ppsspp_batch_cancel. Value initial cap lifts 8 MiB → 32 MiB "
                "in background mode."
            ),
        ),
    ] = False,
) -> ScanOutput:
    """PURPOSE: Three-mode memory scanner — byte-pattern search, Cheat-Engine-style value scan with narrowing sessions, and charset-aware string harvesting.

    USAGE: mode='pattern' + pattern + start_addr/end_addr (migrated from read_memory scan); mode='value' + phase='initial'/value/width → handle, then phase='narrow'/op/value to converge, 'list'/'drop' to manage; mode='strings' + charset + start_addr/end_addr → [{address, text}]. background=true submits a detached job instead (recommended for full-band scans) and returns {action:'submitted', batch_id, ...} — poll ppsspp_batch_status(batch_id=...).

    BEHAVIOR: READ-ONLY. Large ranges are read across multiple reads; unreadable regions are skipped per-chunk (one WS round-trip each). Value sessions live in a bounded per-server registry (cap 4, FIFO), are bound to the creating session, and the initial-scan cap is 8 MiB foreground / 32 MiB background — a full-band 24 MB scan takes ~40 s over localhost WS (measured, 64 KiB chunks), so use background=true beyond ~1 MiB. The registry is process-global: parallel sessions share one cap and FIFO order, so another session's scans can evict your handle under load.

    ROUTING: what-changed-between-two-points -> ppsspp_diff_memory (snapshots); who-accesses-this-address -> ppsspp_breakpoint(action='trace'); value candidates with known addresses -> read_memory directly.

    RETURNS: pattern → {action, address, value: [matches], size}; value initial → {scan_handle, width, candidates, passes}; value narrow → {scan_handle, candidates, passes}; value list → {scan_handle, addresses: [...]}; value drop → {scan_handle, dropped}; strings → {charset, count, strings: [{address, text}]}; background=true submission → {action: 'submitted', batch_id, session_id, estimated_s}."""
    session_id_resolved: str | None = None
    if mode in ("pattern", "strings") or (mode == "value" and phase in ("initial", "narrow")):
        session_id_resolved = await resolve_session_id(session_id)
    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_scan", "mode": mode, "session_id": session_id_resolved},
    )
    try:
        if background:
            # 🟡10: the background runner hardcodes phase='initial'. A
            # narrow/list/drop request submitted as background would be
            # silently rewritten into a fresh initial scan — reject it
            # instead of corrupting the caller's scan session.
            if mode == "value" and phase is not None and phase != "initial":
                raise ArgsInvalid(
                    f"background value scans only support phase='initial' "
                    f"(got {phase!r}) — run narrow/list/drop in the foreground"
                )
            # ── 后台路径：校验/预解析后提交 detached 任务，立即返回 ──
            from ppsspp_dfx_mcp.core.batch_jobs import get_registry

            if not start_addr or not end_addr:
                raise ArgsInvalid("background scans require start_addr and end_addr")
            start_int = parse_address(start_addr)
            end_int = parse_address(end_addr)
            if start_int <= 0 or end_int <= start_int:
                raise ArgsInvalid(f"invalid range: {start_addr!r}..{end_addr!r}")
            total = end_int - start_int
            bg_cap = _VALUE_BG_HARD_RANGE if mode == "value" else MAX_SCAN_RANGE_BYTES
            if total > bg_cap:
                raise ArgsInvalid(
                    f"scan range {total} bytes exceeds the background cap "
                    f"{bg_cap} — narrow start/end"
                )
            await validate_session_alive(session_id_resolved)
            registry = get_registry()
            existing = registry.running_job_for_session(session_id_resolved)
            if existing is not None:
                raise ArgsInvalid(
                    f"session {session_id_resolved} already has a background "
                    f"job ({existing.batch_id}) queued/running — poll "
                    f"ppsspp_batch_status(batch_id='{existing.batch_id}') or "
                    f"cancel it before submitting another"
                )

            total_chunks = max(1, -(-total // MAX_SINGLE_READ_BYTES))
            estimated_s = round(total_chunks * 0.05 + 0.5, 2)

            async def _bg_runner(job: Any) -> dict[str, Any]:
                if mode == "pattern":
                    out = await _scan_pattern(
                        session_id_resolved,
                        pattern,
                        pattern_type,
                        start_addr,
                        end_addr,
                        max_results,
                        chunk_size,
                    )
                elif mode == "value":
                    out = await _scan_value(
                        session_id_resolved,
                        "initial",
                        value,
                        width,
                        op,
                        None,
                        start_addr,
                        end_addr,
                        hard_range=_VALUE_BG_HARD_RANGE,
                    )
                else:
                    out = await _scan_strings(
                        session_id_resolved,
                        charset,
                        start_addr,
                        end_addr,
                        min_len,
                        quality,
                    )
                resp = out.model_dump(mode="json")
                job.result = resp  # 先存再抛（轮询者可见部分结果）
                return resp

            batch_id = get_registry().submit(session_id_resolved, total_chunks, _bg_runner)
            return {
                "action": "submitted",
                "batch_id": batch_id,
                "session_id": session_id_resolved,
                "estimated_s": estimated_s,
                "hint": (
                    "poll ppsspp_batch_status(batch_id=...) — the scan keeps "
                    "running even if this client call times out; cancel via "
                    "ppsspp_batch_cancel(batch_id=...)"
                ),
            }
        if mode == "pattern":
            return (
                await _scan_pattern(
                    session_id_resolved,
                    pattern,
                    pattern_type,
                    start_addr,
                    end_addr,
                    max_results,
                    chunk_size,
                )
            ).model_dump(mode="json")
        if mode == "value":
            return (
                await _scan_value(
                    session_id_resolved,
                    phase,
                    value,
                    width,
                    op,
                    scan_handle,
                    start_addr,
                    end_addr,
                )
            ).model_dump(mode="json")
        # mode == "strings"
        return (
            await _scan_strings(
                session_id_resolved,
                charset,
                start_addr,
                end_addr,
                min_len,
                quality,
            )
        ).model_dump(mode="json")
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e


async def _scan_pattern(
    session_id: str | None,
    pattern: str | None,
    pattern_type: str,
    start_addr: str | None,
    end_addr: str | None,
    max_results: int,
    chunk_size: int,
) -> ScanOutput:
    if not pattern:
        raise ArgsInvalid("pattern is required for mode='pattern'")
    if max_results <= 0:
        raise ArgsInvalid(f"max_results must be > 0 (got {max_results})")
    if chunk_size <= 0:
        raise ArgsInvalid(f"chunk_size must be > 0 (got {chunk_size})")
    if not start_addr or not end_addr:
        raise ArgsInvalid("start_addr and end_addr are required for mode='pattern'")
    start_int = parse_address(start_addr)
    end_int = parse_address(end_addr)
    if start_int >= end_int:
        raise ArgsInvalid(f"start_addr (0x{start_int:08X}) must be < end_addr (0x{end_int:08X})")
    if end_int - start_int > MAX_SCAN_RANGE_BYTES:
        raise ArgsInvalid(
            f"scan range too large: 0x{start_int:08X}-0x{end_int:08X} "
            f"({end_int - start_int} bytes; cap 256 MiB). Narrow start/end."
        )
    chunk_size = max(MIN_SCAN_CHUNK_BYTES, min(chunk_size, MAX_SINGLE_READ_BYTES))
    pattern_bytes = _decode_pattern(pattern, pattern_type)
    if len(pattern_bytes) > MAX_SCAN_PATTERN_BYTES:
        raise ArgsInvalid(
            f"pattern is {len(pattern_bytes)} bytes; the scan cap is "
            f"{MAX_SCAN_PATTERN_BYTES} bytes. Narrow the pattern."
        )
    async with session_client(session_id) as client:
        matches = await client.scan_memory(
            pattern=pattern_bytes,
            start=start_int,
            end=end_int,
            max_results=max_results,
            chunk_size=chunk_size,
        )
    return ScanResponse.build_pattern(start_int, matches or [])


async def _scan_value(
    session_id: str | None,
    phase: str | None,
    value: int | None,
    width: str,
    op: str,
    scan_handle: str | None,
    start_addr: str | None,
    end_addr: str | None,
    hard_range: int = _VALUE_HARD_RANGE,
) -> ScanOutput:
    if phase is None:
        raise ArgsInvalid("mode='value' requires phase (initial/narrow/list/drop)")
    size, fmt = _WIDTHS[width]

    if phase == "initial":
        if value is None:
            raise ArgsInvalid("phase='initial' requires value")
        if not start_addr or not end_addr:
            raise ArgsInvalid("phase='initial' requires start_addr and end_addr")
        start_int = parse_address(start_addr)
        end_int = parse_address(end_addr)
        if start_int <= 0 or end_int <= start_int:
            raise ArgsInvalid(f"invalid range: {start_addr!r}..{end_addr!r}")
        total = end_int - start_int
        if total > hard_range:
            raise ArgsInvalid(
                f"initial scan range {total} bytes exceeds the hard cap "
                f"{hard_range} — narrow the range (a full-band 24 MB "
                f"scan takes ~40 s over localhost WS; keep ≤1 MiB per "
                f"call or split)."
            )
        async with session_client(session_id) as client:
            segments = await _read_segments(client, start_int, total)
        candidates: list[int] = []
        for seg_start, blob in segments:
            for off in range(0, len(blob) - size + 1):
                if _cmp(struct.unpack("<" + fmt, blob[off : off + size])[0], op, value):
                    candidates.append(seg_start + off)
                    if len(candidates) >= _VALUE_MAX_HITS:
                        break
            if len(candidates) >= _VALUE_MAX_HITS:
                break
        _evict_oldest_if_full()
        handle = uuid.uuid4().hex[:8]
        _VALUE_SESSIONS[handle] = {
            "session_id": session_id,
            "width": width,
            "addresses": candidates,
            "created_at": time.time(),
            "passes": 1,
        }
        return ScanResponse.build_value_initial(handle, width, len(candidates))

    if not scan_handle:
        raise ArgsInvalid(f"phase='{phase}' requires scan_handle")
    sess = _VALUE_SESSIONS.get(scan_handle)
    if sess is None:
        raise ArgsInvalid(
            f"unknown scan_handle {scan_handle!r} — use phase='initial' "
            f"(sessions are per-server-process, cap {_MAX_VALUE_SESSIONS} FIFO)"
        )

    if phase == "narrow":
        if value is None:
            raise ArgsInvalid("phase='narrow' requires value")
        if sess.get("session_id") != session_id:
            raise ArgsInvalid(
                f"scan_handle {scan_handle!r} belongs to session "
                f"{sess.get('session_id')!r}, not {session_id!r} — "
                f"drop it and re-scan in this session"
            )
        sess["addresses"] = await _narrow_candidates(
            session_id, sess["addresses"], value, _WIDTHS[sess["width"]][0], op
        )
        sess["passes"] += 1
        return ScanResponse.build_value_narrow(scan_handle, len(sess["addresses"]), sess["passes"])

    if phase == "list":
        return ScanResponse.build_value_list(scan_handle, sess["addresses"], sess["width"])

    # phase == "drop"
    del _VALUE_SESSIONS[scan_handle]
    return ScanResponse.build_value_drop(scan_handle)


async def _scan_strings(
    session_id: str | None,
    charset: str,
    start_addr: str | None,
    end_addr: str | None,
    min_len: int,
    quality: float,
) -> ScanOutput:
    if not start_addr or not end_addr:
        raise ArgsInvalid("start_addr and end_addr are required for mode='strings'")
    start_int = parse_address(start_addr)
    end_int = parse_address(end_addr)
    if start_int <= 0 or end_int <= start_int:
        raise ArgsInvalid(f"invalid range: {start_addr!r}..{end_addr!r}")
    total = end_int - start_int
    if total > MAX_SCAN_RANGE_BYTES:
        raise ArgsInvalid(f"range too large: {total} bytes (cap {MAX_SCAN_RANGE_BYTES})")
    async with session_client(session_id) as client:
        segments = await _read_segments(client, start_int, total)
    run_re = re.compile(_RUNS[charset].pattern.replace(b"{6,}", f"{{{max(min_len, 1)},}}".encode()))
    decode = _DECODERS[charset]

    def _jp_ratio(s: str) -> float:
        if not s:
            return 0.0
        cjk = sum(1 for ch in s if "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff")
        return cjk / len(s)

    strings_out: list[dict[str, Any]] = []
    for seg_start, data in segments:
        for m in run_re.finditer(data):
            if m.end() - m.start() < min_len:
                continue
            try:
                s = decode(m.group())
            except UnicodeDecodeError:
                continue
            if charset == "shift_jis" and quality > 0 and _jp_ratio(s) < quality:
                continue
            strings_out.append({"address": seg_start + m.start(), "text": s})

    return ScanResponse.build_strings(charset, strings_out)


async def _narrow_candidates(
    session_id: str, addresses: list[int], value: int, size: int, op: str
) -> list[int]:
    """Re-read candidate addresses in merged span reads: N sequential WS
    round-trips collapse to O(runs) (candidate offsets still evaluated
    exactly, so semantics match the per-address reader). Runs whose read
    fails fall back to per-address reads (partial unmapped spans)."""
    if not addresses:
        return []
    fmt = "<" + {1: "B", 2: "H", 4: "I"}[size]
    out: list[int] = []
    async with session_client(session_id) as client:
        for run_start, span, addrs in _merge_runs(addresses, size):
            try:
                blob = bytes(await client.read_bytes(address=run_start, size=span))
            except Exception:
                for addr in addrs:
                    try:
                        raw = await client.read_bytes(address=addr, size=size)
                    except Exception:
                        continue
                    if _cmp(struct.unpack(fmt, bytes(raw))[0], op, value):
                        out.append(addr)
                continue
            for addr in addrs:
                off = addr - run_start
                if _cmp(struct.unpack(fmt, blob[off : off + size])[0], op, value):
                    out.append(addr)
    return out


def _evict_oldest_if_full() -> None:
    while len(_VALUE_SESSIONS) >= _MAX_VALUE_SESSIONS:
        oldest = next(iter(_VALUE_SESSIONS))
        _VALUE_SESSIONS.pop(oldest)
        logger.info("value scan session evicted (FIFO): %s", oldest)
