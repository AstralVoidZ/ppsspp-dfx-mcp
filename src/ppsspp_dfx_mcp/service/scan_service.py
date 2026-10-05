"""Memory-scan orchestration — extracted from PpssppDebugClient.

``scan_memory`` is business orchestration over ``read_bytes`` (chunked
reads + Python ``bytes.find``), NOT a WS event wrapper: the PPSSPP WS
debugger has no ``memory.scan`` event. It lives here as a module-level
function that receives the ``read_bytes`` callable so it can be tested
and reasoned about without the full client; ``PpssppDebugClient`` keeps
a thin delegating method with the identical public signature.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ppsspp_dfx_mcp.core.primitives import (
    MAX_SINGLE_READ_BYTES,
    SCAN_MAX_CONSECUTIVE_READ_FAILURES,
    SCAN_READ_TIMEOUT_S,
)


async def scan_memory(
    read_bytes: Callable[..., Awaitable[bytes]],
    pattern: bytes,
    start: int,
    end: int,
    max_results: int = 100,
    chunk_size: int = 4096,
) -> list[dict[str, Any]]:
    """Client-side memory scan via chunked read_bytes + bytes.find.

    Extracted implementation of ``PpssppDebugClient.scan_memory`` — see
    that method for the full contract docstring.
    """
    if not pattern:
        return []
    if end <= start:
        return []

    matches: list[dict[str, Any]] = []
    overlap = len(pattern) - 1
    # Per-chunk read timeout + consecutive-timeout abort (v0.1.7): a
    # wedged PPSSPP must fail the scan cleanly instead of holding the
    # session lock forever (plain exceptions = legitimately unmapped
    # regions and are still skipped silently; only TIMEOUTS count).
    consecutive_timeouts = 0
    # Each iteration issues ONE memory.read of
    # chunk + overlap bytes. The tool layer caps the pattern at 4096
    # bytes, but direct client callers can pass any length — clamp the
    # effective chunk so a single read stays within the documented
    # 64 KiB budget (clamping chunk_size alone did not cover
    # the overlap tail).
    effective_chunk = max(1, min(chunk_size, MAX_SINGLE_READ_BYTES - overlap))
    cursor = start

    while cursor < end and len(matches) < max_results:
        # Read effective_chunk bytes; reserve overlap for boundary-
        # spanning patterns. The last chunk does not extend beyond `end`.
        chunk_end = min(cursor + effective_chunk, end)
        read_end = min(chunk_end + overlap, end) if chunk_end < end else chunk_end
        read_size = read_end - cursor
        if read_size <= 0:
            break

        try:
            data = await asyncio.wait_for(
                read_bytes(address=cursor, size=read_size),
                timeout=SCAN_READ_TIMEOUT_S,
            )
        except TimeoutError as e:
            consecutive_timeouts += 1
            if consecutive_timeouts > SCAN_MAX_CONSECUTIVE_READ_FAILURES:
                raise RuntimeError(
                    f"memory scan aborted: {consecutive_timeouts} consecutive "
                    f"chunk reads timed out ({SCAN_READ_TIMEOUT_S}s each) at "
                    f"0x{cursor:08X} — PPSSPP appears wedged; the session "
                    f"lock is released"
                ) from e
            cursor = chunk_end
            continue
        except Exception:
            # Skip unreadable regions (e.g., unmapped, permission
            # denied). The scan continues at the next chunk boundary.
            cursor = chunk_end
            continue

        consecutive_timeouts = 0
        # Search for all occurrences in this chunk, but only report
        # matches within [cursor, chunk_end) to avoid duplicates in
        # the overlap region (overlap bytes are re-read next iteration).
        search_start = 0
        while search_start < len(data):
            idx = data.find(pattern, search_start)
            if idx == -1:
                break
            match_addr = cursor + idx
            if match_addr >= chunk_end:
                break  # in overlap tail; next chunk will find it
            if match_addr < end:
                context = data[idx : idx + len(pattern)].hex().upper()
                matches.append(
                    {
                        "address": match_addr,
                        "context": context,
                    }
                )
                if len(matches) >= max_results:
                    break
            search_start = idx + 1

        # Advance to the next chunk boundary (overlap is re-read on
        # the next iteration to catch boundary-spanning patterns).
        # Use actual bytes read to avoid skipping memory on short reads.
        cursor = cursor + len(data) if len(data) < read_size else chunk_end

    return matches
