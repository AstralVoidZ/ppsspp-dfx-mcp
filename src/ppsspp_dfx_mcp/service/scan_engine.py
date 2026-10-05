"""Pattern-scan engine — extracted from ``tools/scan.py`` (W19).

Holds the pure pattern-mode algorithm (cap/chunk validation + hex/ascii
decoding + the ``scan_memory`` round-trip) and the merged-run grouping
helper shared with ``tools/state_observer.py`` and ``tools/batch_step.py``.
Moving it here keeps the MCP tool body (`ppsspp_scan`) as dispatch + output
assembly instead of a mix of transport and algorithm.

The scan caps live here with the algorithm they bound; ``tools/_common.py``
no longer owns them (they had no other consumer). ``_scan_pattern`` keeps
its name because the lock-contract tripwire classifies ``ppsspp_scan`` by
the delegated opener name it calls.
"""

from __future__ import annotations

from typing import Any

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.core.primitives import MAX_SINGLE_READ_BYTES
from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.views.scan import ScanResponse

# ── Scan bounds (moved here from tools/_common.py) ──────────────────────
MIN_SCAN_CHUNK_BYTES = 64  # scan chunk lower clamp
MAX_SCAN_RANGE_BYTES = 256 * 1024 * 1024  # scan range upper cap
# Scan reads chunk + len(pattern)-1 bytes in ONE memory.read, so an
# unbounded pattern silently exceeds the 64 KiB single-read budget the
# tool layer documents (symptom: an "empty successful scan"). Real-PPSSPP
# probes showed 44KiB reads succeed, so this is a contract-consistency
# cap, not a hard server limit.
MAX_SCAN_PATTERN_BYTES = 4096


def _require_int(value: Any, name: str) -> int:
    """Guard: reject ``bool`` where an ``int`` is expected.

    Local copy of ``tools/_common.require_int_not_bool`` (the tool-layer
    helper cannot be imported here — service must not depend on tools).
    Same message, same semantics.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArgsInvalid(f"{name} must be an int (bool is not accepted); got {value!r}")
    return value


def _decode_hex_pattern(pattern: str) -> bytes:
    try:
        return bytes.fromhex(pattern.replace(" ", ""))
    except ValueError as e:
        raise ArgsInvalid(
            f"pattern is not a valid hex string: {e} — "
            f"hex bytes like 'AABBCCDD' or pattern_type='ascii'"
        ) from e


def _decode_pattern(pattern: str, pattern_type: str) -> bytes:
    if pattern_type == "ascii":
        return pattern.encode("ascii")
    return _decode_hex_pattern(pattern)


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


async def _scan_pattern(
    session_id: str,
    pattern: str | None,
    pattern_type: str,
    start_addr: str | None,
    end_addr: str | None,
    max_results: int,
    chunk_size: int,
) -> ScanResponse:
    if not pattern:
        raise ArgsInvalid("pattern is required for mode='pattern'")
    max_results = _require_int(max_results, "max_results")
    if max_results <= 0:
        raise ArgsInvalid(f"max_results must be > 0 (got {max_results})")
    chunk_size = _require_int(chunk_size, "chunk_size")
    if chunk_size <= 0:
        raise ArgsInvalid(f"chunk_size must be > 0 (got {chunk_size})")
    if not start_addr or not end_addr:
        raise ArgsInvalid("start_addr and end_addr are required for mode='pattern'")
    start_int = parse_address(start_addr)
    end_int = parse_address(end_addr)
    # A-1 (review v4): same verdict as the background/value/strings paths —
    # start=0x0 is NULL and not a scan target. The foreground path was the
    # only one accepting it.
    if start_int <= 0 or end_int <= start_int:
        raise ArgsInvalid(f"invalid range: {start_addr!r}..{end_addr!r}")
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


__all__ = [
    "MAX_SCAN_PATTERN_BYTES",
    "MAX_SCAN_RANGE_BYTES",
    "MIN_SCAN_CHUNK_BYTES",
    "_merge_runs",
    "_scan_pattern",
]
