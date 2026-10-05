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

import asyncio
import logging
import re
import struct
import time
import uuid
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypedDict

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.core.primitives import MAX_SINGLE_READ_BYTES
from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.service.scan_engine import (
    MAX_SCAN_RANGE_BYTES,
    _merge_runs,
    _scan_pattern,
)
from ppsspp_dfx_mcp.session.client_helper import (
    resolve_session_id,
    session_client,
    validate_session_alive,
)
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract, flatten_union
from ppsspp_dfx_mcp.tools._common import (
    FOREGROUND_SCAN_LIMIT_BYTES,
    SCAN_BG_BUDGET_S,
    SCAN_MAX_CONSECUTIVE_READ_FAILURES,
    SCAN_READ_TIMEOUT_S,
    translate_tool_errors,
)
from ppsspp_dfx_mcp.views.scan import ScanResponse

logger = logging.getLogger(__name__)

_ScanResponseOut = derive_output_contract("ScanResponseOut", ScanResponse, partial=True)


class _BackgroundSubmitKeys(TypedDict, total=False):
    """Bare-dict shape returned by the background submit branch.

    That path bypasses ScanResponse, so every key name it emits must be
    listed here explicitly to keep the flattened ScanOutput contract
    complete.
    """

    action: str
    batch_id: str
    session_id: str
    estimated_s: float
    hint: str


# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    ScanOutput = dict[str, Any]
else:
    ScanOutput = flatten_union("ScanOutput", _ScanResponseOut, _BackgroundSubmitKeys)
# ── value-scan session registry ──────────────────────────────────────────
_VALUE_SESSIONS: dict[str, dict[str, Any]] = {}
_MAX_VALUE_SESSIONS = 4
_VALUE_DEFAULT_RANGE = 1 << 20  # 1 MiB initial-scan soft cap
_VALUE_HARD_RANGE = 8 << 20  # 8 MiB foreground hard cap
_VALUE_BG_HARD_RANGE = 32 << 20  # 32 MiB background hard cap (full band + slack)
_VALUE_MAX_HITS = 5000
# strings-mode caps — hit count and per-hit text.
_MAX_STRINGS = 500
_MAX_TEXT_CHARS = 4096

_WIDTHS = {"u8": (1, "B"), "u16": (2, "H"), "u32": (4, "I")}
# Precompiled little-endian readers: building "<"+fmt and slicing
# per element ran on the event loop and dominated 8 MiB scans.
_STRUCTS = {fmt: struct.Struct("<" + fmt) for _, fmt in _WIDTHS.values()}
_FMT_BY_SIZE = {1: "B", 2: "H", 4: "I"}
_OPS = ("eq", "ne", "lt", "gt")


def reset_value_sessions() -> None:
    """清空值扫描会话表（语义化回收 API；长驻进程的内存回收面）。"""
    _VALUE_SESSIONS.clear()


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


def _scan_block_hits(blob: bytes, fmt: str, op: str, value: int, limit: int) -> list[int]:
    """Offsets inside ``blob`` whose little-endian <fmt> value satisfies ``op``.

    The per-element ``struct.unpack("<"+fmt, blob[off:off+n])``
    loop ran on the event loop (measured 1 MiB u16 ≈ 0.168 s → an 8 MiB
    foreground scan ≈ 1.3 s of stalled event loop). Two fast paths:
    ``op == "eq"`` searches the encoded target with ``bytes.find`` (C-level),
    every other op uses the precompiled ``Struct.unpack_from`` (no slicing).
    Returned offsets are identical to the naive per-offset loop, including
    overlapping matches, and stop at ``limit`` hits.
    """
    out: list[int] = []
    st = _STRUCTS[fmt]
    size = st.size
    if limit <= 0 or len(blob) < size:
        return out
    if op == "eq":
        try:
            target = value.to_bytes(size, "little", signed=False)
        except (OverflowError, ValueError):
            # The value cannot exist at this width — the naive comparison
            # would never match either.
            return out
        start = 0
        while len(out) < limit:
            idx = blob.find(target, start)
            if idx < 0:
                break
            out.append(idx)
            start = idx + 1  # +1 keeps overlapping matches (naive semantics)
        return out
    unpack_from = st.unpack_from
    for off in range(len(blob) - size + 1):
        if _cmp(unpack_from(blob, off)[0], op, value):
            out.append(off)
            if len(out) >= limit:
                break
    return out


async def _iter_segments(client: Any, start: int, size: int):
    """Yield ``(seg_start, bytes)`` readable chunks over ``[start, start+size)``.

    This used to append every chunk to a list and return it,
    so a 256 MiB strings scan held the whole range in memory. Streaming keeps
    the peak at one read chunk. Unreadable chunks are SKIPPED — the
    ppsspp_scan BEHAVIOR contract — and segment pairs (not one flat buffer)
    keep addresses exact when a hole splits the range.
    """
    # Per-read timeout + consecutive-timeout abort (v0.1.7): only
    # TIMEOUTS count toward the abort — plain exceptions are legitimately
    # unmapped regions and stay silent skips.
    consecutive_timeouts = 0
    for offset in range(0, size, MAX_SINGLE_READ_BYTES):
        chunk = min(MAX_SINGLE_READ_BYTES, size - offset)
        try:
            raw = await asyncio.wait_for(
                client.read_bytes(address=start + offset, size=chunk),
                timeout=SCAN_READ_TIMEOUT_S,
            )
        except TimeoutError as e:
            consecutive_timeouts += 1
            if consecutive_timeouts > SCAN_MAX_CONSECUTIVE_READ_FAILURES:
                raise RuntimeError(
                    f"scan aborted: {consecutive_timeouts} consecutive chunk "
                    f"reads timed out ({SCAN_READ_TIMEOUT_S}s each) at "
                    f"0x{start + offset:08X} — PPSSPP appears wedged"
                ) from e
            continue
        except Exception:
            continue
        consecutive_timeouts = 0
        yield (start + offset, bytes(raw))


# LONG-TOOL: the three scan modes share one bounded value-session registry and one timeout-aware
# read loop, so the dispatch is kept with that shared state.
@mcp.tool(
    name="ppsspp_scan",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=False
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
                "Active session ID; auto-resolved when exactly one "
                "session is active. Required for the pattern / value "
                "initial / strings phases, which start a new scan. The "
                "narrow / list / drop phases only re-read addresses "
                "already recorded by an earlier phase, so they do not "
                "need it passed -- but it must still resolve to the "
                "same session."
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
            default=65536,
            description=(
                "Bytes per read request during chunked scans (default "
                "65536 — measured ~6x faster end-to-end than the old "
                "4096 default; clamped to [64, 65536])."
            ),
        ),
    ] = 65536,
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
                "Run as a detached background job: returns a batch_id "
                "immediately; poll ppsspp_batch_status(batch_id=...), "
                "cancel via ppsspp_batch_cancel. Value initial cap lifts "
                "8 MiB → 32 MiB in background mode. NOTE: pattern/strings "
                "ranges over 2 MiB are auto-backgrounded even when this "
                "is false — a foreground scan that outlives the ~30s "
                "client timeout is the classic 'frozen session' trap."
            ),
        ),
    ] = False,
) -> ScanOutput:
    """PURPOSE: Three-mode memory scanner — byte-pattern search, Cheat-Engine-style value scan with narrowing sessions, and charset-aware string harvesting.

    USAGE: mode='pattern' + pattern + start_addr/end_addr (migrated from read_memory scan); mode='value' + phase='initial'/value/width → handle, then phase='narrow'/op/value to converge, 'list'/'drop' to manage; mode='strings' + charset + start_addr/end_addr → [{address, text}]. background=true submits a detached job instead (recommended for full-band scans) and returns {action:'submitted', batch_id, ...} — poll ppsspp_batch_status(batch_id=...).

    BEHAVIOR: READ-ONLY. Large ranges are read across multiple reads; unreadable regions are skipped per-chunk (one WS round-trip each), but CONSECUTIVE read timeouts (10 s each, >5 in a row) abort the scan — a wedged PPSSPP fails the scan cleanly instead of pinning the session lock. pattern/strings ranges over 2 MiB are AUTO-BACKGROUNDED (returns {action:'submitted', batch_id, ...} even with background=false) — measured: 24 MB @ 4 KiB chunks takes 53-96 s depending on PPSSPP build, always past the ~30s client timeout, while @ 64 KiB chunks it is 3.4-40 s (build-dependent). Value sessions live in a bounded per-server registry (cap 4, FIFO), are bound to the creating session, and the initial-scan cap is 8 MiB foreground / 32 MiB background. Background scans carry a 600 s wall-clock budget; exceeding it fails the job and releases the session. The registry is process-global: parallel sessions share one cap and FIFO order, so another session's scans can evict your handle under load.

    ROUTING: what-changed-between-two-points -> ppsspp_diff_memory (snapshots); who-accesses-this-address -> ppsspp_breakpoint(action='trace'); value candidates with known addresses -> read_memory directly.

    RETURNS: pattern → {action, address, value: [matches], size}; value initial → {scan_handle, width, candidates, passes}; value narrow → {scan_handle, candidates, passes}; value list → {scan_handle, addresses: [...]}; value drop → {scan_handle, dropped}; strings → {charset, count, strings: [{address, text}]}; background submission (explicit background=true OR pattern/strings range > 2 MiB) → {action: 'submitted', batch_id, session_id, estimated_s}."""
    # value 模式必须给出四个 phase 之一——请求本身非法时在解析会话之前早拒。
    # 顺序关键：此前 phase=None 会被放行到后台分支，以 None 会话调用
    # validate_session_alive(None)，报出与真实原因无关的 SessionNotFound。
    if mode == "value" and phase not in ("initial", "narrow", "list", "drop"):
        raise ArgsInvalid(f"mode='value' requires phase (initial/narrow/list/drop); got {phase!r}")
    # 到这里三种模式都必然需要会话：pattern/strings 扫描，value 的四个 phase。
    session_id_resolved = await resolve_session_id(session_id)
    # Auto-background guard (v0.1.7, real-PPSSPP evidence): a foreground
    # scan runs inside the MCP request scope, and ranges beyond
    # FOREGROUND_SCAN_LIMIT_BYTES measurably outlive the ~30s client
    # timeout (24 MB @ 4 KiB chunks ≈ 53-96 s) — the client cancels,
    # the agent retries, and the loop *looks* like a frozen session.
    # Route those to the detached background job path transparently.
    if (
        not background
        and mode in ("pattern", "strings")
        and start_addr
        and end_addr
        and parse_address(end_addr) - parse_address(start_addr) > FOREGROUND_SCAN_LIMIT_BYTES
    ):
        background = True
        logger.info(
            "scan auto-backgrounded: range exceeds %d bytes",
            FOREGROUND_SCAN_LIMIT_BYTES,
        )
    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_scan", "mode": mode, "session_id": session_id_resolved},
    )
    if background:
        # The background runner hardcodes phase='initial'. A
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
                f"scan range {total} bytes exceeds the background cap {bg_cap} — narrow start/end"
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
            # Wall-clock budget (v0.1.7): the detached task holds the
            # session lock for its whole duration, so a runaway scan
            # must reach a terminal state on its own — a timeout
            # fails the job cleanly and the lock is released (the
            # field-report wedge where only stop/restart helped).
            async def _run() -> ScanResponse:
                if mode == "pattern":
                    return await _scan_pattern(
                        session_id_resolved,
                        pattern,
                        pattern_type,
                        start_addr,
                        end_addr,
                        max_results,
                        chunk_size,
                    )
                if mode == "value":
                    return await _scan_value(
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
                return await _scan_strings(
                    session_id_resolved,
                    charset,
                    start_addr,
                    end_addr,
                    min_len,
                    quality,
                )

            try:
                out = await asyncio.wait_for(_run(), timeout=SCAN_BG_BUDGET_S)
            except TimeoutError as e:
                raise ToolError(
                    f"background scan exceeded its {SCAN_BG_BUDGET_S:.0f}s "
                    f"wall-clock budget and was aborted (session released) "
                    f"— narrow the range or split the scan",
                    code="SCAN_BUDGET_EXCEEDED",
                ) from e
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


def _validate_unsigned_value(value: int, size: int, width: str) -> int:
    """A-6 (review v4): the value must be a non-bool int that fits the
    unsigned width. Negative values used to fall into the OverflowError
    branch and silently return "0 candidates" for eq; width overflow did
    the same — the caller read it as "value not in range" and kept
    narrowing instead of fixing the argument."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArgsInvalid(f"value must be an integer, got {value!r}")
    if value < 0 or value >= (1 << (8 * size)):
        raise ArgsInvalid(
            f"value {value} does not fit unsigned {width} — expected an "
            f"integer in [0, {(1 << (8 * size)) - 1}]"
        )
    return value


async def _scan_value(
    session_id: str,
    phase: str | None,
    value: int | None,
    width: str,
    op: str,
    scan_handle: str | None,
    start_addr: str | None,
    end_addr: str | None,
    hard_range: int = _VALUE_HARD_RANGE,
) -> ScanResponse:
    if phase is None:
        raise ArgsInvalid("mode='value' requires phase (initial/narrow/list/drop)")
    size, fmt = _WIDTHS[width]

    if phase == "initial":
        if value is None:
            raise ArgsInvalid("phase='initial' requires value")
        _validate_unsigned_value(value, size, width)
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
        candidates: list[int] = []
        async with session_client(session_id) as client:
            async for seg_start, blob in _iter_segments(client, start_int, total):
                remaining = _VALUE_MAX_HITS - len(candidates)
                if remaining <= 0:
                    break
                for off in _scan_block_hits(blob, fmt, op, value, remaining):
                    candidates.append(seg_start + off)
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
        _validate_unsigned_value(value, _WIDTHS[sess["width"]][0], sess["width"])
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

    # list/drop used to bypass the ownership check that
    # narrow enforces — session A could list or drop session B's handle,
    # contradicting the tool's "bound to the creating session" contract.
    if sess.get("session_id") != session_id:
        raise ArgsInvalid(
            f"scan_handle {scan_handle!r} belongs to session "
            f"{sess.get('session_id')!r}, not {session_id!r} — "
            f"drop it and re-scan in this session"
        )

    if phase == "list":
        return ScanResponse.build_value_list(scan_handle, sess["addresses"], sess["width"])

    # phase == "drop"
    del _VALUE_SESSIONS[scan_handle]
    return ScanResponse.build_value_drop(scan_handle)


async def _scan_strings(
    session_id: str,
    charset: str,
    start_addr: str | None,
    end_addr: str | None,
    min_len: int,
    quality: float,
) -> ScanResponse:
    if not start_addr or not end_addr:
        raise ArgsInvalid("start_addr and end_addr are required for mode='strings'")
    start_int = parse_address(start_addr)
    end_int = parse_address(end_addr)
    if start_int <= 0 or end_int <= start_int:
        raise ArgsInvalid(f"invalid range: {start_addr!r}..{end_addr!r}")
    total = end_int - start_int
    if total > MAX_SCAN_RANGE_BYTES:
        raise ArgsInvalid(f"range too large: {total} bytes (cap {MAX_SCAN_RANGE_BYTES})")
    run_re = re.compile(_RUNS[charset].pattern.replace(b"{6,}", f"{{{max(min_len, 1)},}}".encode()))
    decode = _DECODERS[charset]

    def _jp_ratio(s: str) -> float:
        if not s:
            return 0.0
        cjk = sum(1 for ch in s if "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff")
        return cjk / len(s)

    strings_out: list[dict[str, Any]] = []
    truncated = False
    async with session_client(session_id) as client:
        async for seg_start, data in _iter_segments(client, start_int, total):
            for m in run_re.finditer(data):
                if m.end() - m.start() < min_len:
                    continue
                try:
                    s = decode(m.group())
                except UnicodeDecodeError:
                    continue
                if charset == "shift_jis" and quality > 0 and _jp_ratio(s) < quality:
                    continue
                # strings was the one unbounded scan mode —
                # a single printable run can be megabytes (8MB of 'A' is one
                # "string") and the full result used to persist in the
                # background-job registry, re-serialized on every status
                # poll. Cap hits and per-hit text.
                if len(strings_out) >= _MAX_STRINGS:
                    truncated = True
                    break
                strings_out.append(
                    {
                        "address": seg_start + m.start(),
                        "text": s[:_MAX_TEXT_CHARS],
                    }
                )
            if truncated:
                break

    return ScanResponse.build_strings(charset, strings_out, truncated=truncated)


async def _narrow_candidates(
    session_id: str, addresses: list[int], value: int, size: int, op: str
) -> list[int]:
    """Re-read candidate addresses in merged span reads: N sequential WS
    round-trips collapse to O(runs) (candidate offsets still evaluated
    exactly, so semantics match the per-address reader). Runs whose read
    fails fall back to per-address reads (partial unmapped spans)."""
    if not addresses:
        return []
    fmt = _FMT_BY_SIZE[size]
    unpack_from = _STRUCTS[fmt].unpack_from
    out: list[int] = []
    async with session_client(session_id) as client:
        for run_start, span, addrs in _merge_runs(addresses, size):
            try:
                blob = bytes(await client.read_bytes(address=run_start, size=span))
            except Exception:
                for addr in addrs:
                    try:
                        raw = bytes(await client.read_bytes(address=addr, size=size))
                    except Exception:
                        continue
                    # a short payload used to reach struct.unpack
                    # and raise struct.error, degrading the whole narrow batch to
                    # [INTERNAL]. Skip the candidate instead.
                    if len(raw) == size and _cmp(unpack_from(raw, 0)[0], op, value):
                        out.append(addr)
                continue
            for addr in addrs:
                off = addr - run_start
                chunk = blob[off : off + size]
                if len(chunk) == size and _cmp(unpack_from(chunk, 0)[0], op, value):
                    out.append(addr)
    return out


def _evict_oldest_if_full() -> None:
    while len(_VALUE_SESSIONS) >= _MAX_VALUE_SESSIONS:
        oldest = next(iter(_VALUE_SESSIONS))
        _VALUE_SESSIONS.pop(oldest)
        logger.info("value scan session evicted (FIFO): %s", oldest)
