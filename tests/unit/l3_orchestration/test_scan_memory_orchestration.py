"""L3 orchestration tests: scan_memory chunked-scan algorithm.

Anchors: B.2 spec §5.3 V022 documented decision (short-read cursor
advance). L3 anchors the chunked-scan algorithm end-to-end;
no separate L4 / unit test currently covers scan_memory.

L3 focus (NOT covered by L4):
- V022 short-read cursor advance: `cursor = cursor + len(data)
  if len(data) < read_size else chunk_end` (debug_client.py:1154)
- Short-read continues from correct position (no skip, no infinite loop)
- Overlap region does NOT produce duplicate matches
- Multi-chunk scan with mixed short/full reads
- Unreadable region advances cursor to chunk_end (no infinite loop)
- Pattern at exact chunk_end boundary

V022 (B.2 §5.3): the short-read cursor advance is a documented
decision — PPSSPP typically either reads the full chunk or raises
an exception, but the defensive short-read path ensures cursor
advances by `len(data)` (not `chunk_end`) to avoid skipping memory.
The theoretical infinite-loop risk (empty short read) does not occur
in practice because PPSSPP returns at least 1 byte or raises.
"""

from __future__ import annotations

import base64

import pytest
from fake_transport import FakeTransport

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

# ============================================================================
# Fixtures (local — L3 scan tests need fine-grained control over reads)
# ============================================================================


@pytest.fixture
def scan_transport() -> FakeTransport:
    """Fresh FakeTransport with no pre-configured responses."""
    return FakeTransport()


@pytest.fixture
def scan_client(scan_transport: FakeTransport) -> PpssppDebugClient:
    """PpssppDebugClient backed by scan_transport."""
    return PpssppDebugClient(scan_transport)


def _make_read_handler(memory_map: dict[int, bytes]):
    """Build a callable `memory.read` response that slices from memory_map.

    `memory_map` is {address: bytes} — when `memory.read` is called with
    (address, size), the callable slices from the matching region.
    Returns {base64: <encoded bytes>}.
    """

    def handler(**params):
        addr = params["address"]
        size = params["size"]
        for region_addr, region_data in memory_map.items():
            if region_addr <= addr < region_addr + len(region_data):
                offset = addr - region_addr
                chunk = region_data[offset : offset + size]
                return {"base64": base64.b64encode(chunk).decode("ascii")}
        return {"base64": ""}

    return handler


def _make_short_read_handler(memory_map: dict[int, bytes], max_bytes: int):
    """Build a `memory.read` handler that returns at most `max_bytes`.

    Simulates V022 short-read: PPSSPP returns fewer bytes than requested.
    The handler returns min(requested_size, max_bytes) bytes per call,
    forcing the scan_memory cursor to advance by len(data) (not chunk_end).
    """

    def handler(**params):
        addr = params["address"]
        size = min(params["size"], max_bytes)
        for region_addr, region_data in memory_map.items():
            if region_addr <= addr < region_addr + len(region_data):
                offset = addr - region_addr
                chunk = region_data[offset : offset + size]
                return {"base64": base64.b64encode(chunk).decode("ascii")}
        return {"base64": ""}

    return handler


# ============================================================================
# V022 short-read cursor advance (B.2 §5.3)
# ============================================================================


class TestShortReadCursorAdvance:
    """V022 §5.3: short-read cursor advances by len(data), not chunk_end.

    The short-read path is: `cursor = cursor + len(data) if len(data) <
    read_size else chunk_end`. This ensures the scan does NOT skip
    memory when PPSSPP returns fewer bytes than requested.

    L3 anchors that:
    1. Short-read advances cursor by len(data) (not chunk_end)
    2. Subsequent reads continue from the advanced cursor
    3. No infinite loop (cursor always advances)
    4. Full-read advances cursor to chunk_end
    """

    async def test_short_read_advances_cursor_by_len_data(self, scan_client, scan_transport):
        """Short-read: cursor += len(data), not chunk_end.

        Setup: chunk_size=16, pattern=2 bytes. The handler returns only
        8 bytes per read (short-read). After the first short-read, the
        cursor must advance by 8 (not 16), so the next read starts at
        cursor+8 (not cursor+16) — ensuring no memory is skipped.
        """
        pattern = b"\xaa\xbb"
        region_addr = 0x08804000
        # 32-byte region with pattern at offset 12 (would be skipped if
        # cursor advanced by chunk_end=16 after a short-read of 8 bytes
        # at offset 0: cursor would jump to 16, missing offset 12).
        region_data = bytearray(b"\x00" * 32)
        region_data[12:14] = pattern
        scan_transport.set_response(
            "memory.read",
            _make_short_read_handler({region_addr: bytes(region_data)}, 8),
        )

        matches = await scan_client.scan_memory(
            pattern=pattern,
            start=region_addr,
            end=region_addr + 32,
            chunk_size=16,
        )

        # Pattern at offset 12 must be found (would be missed if cursor
        # jumped to chunk_end=16 after short-read of 8 bytes).
        assert len(matches) == 1
        assert matches[0]["address"] == region_addr + 12

    async def test_full_read_advances_cursor_to_chunk_end(self, scan_client, scan_transport):
        """Full-read: cursor = chunk_end (not cursor + len(data)).

        When len(data) == read_size, cursor advances to chunk_end. This
        is the normal path (PPSSPP returns the full requested bytes).
        The distinction matters when overlap is involved: chunk_end may
        be less than cursor + read_size (because read_size includes
        overlap bytes that are re-read next iteration).
        """
        pattern = b"\xcc\xdd"
        region_addr = 0x08804000
        # 64-byte region with pattern at offset 32 (start of chunk 2)
        region_data = bytearray(b"\x00" * 64)
        region_data[32:34] = pattern
        scan_transport.set_response(
            "memory.read",
            _make_read_handler({region_addr: bytes(region_data)}),
        )

        matches = await scan_client.scan_memory(
            pattern=pattern,
            start=region_addr,
            end=region_addr + 64,
            chunk_size=16,
        )

        assert len(matches) == 1
        assert matches[0]["address"] == region_addr + 32

    async def test_short_read_no_infinite_loop(self, scan_client, scan_transport):
        """Short-read must always advance cursor (no infinite loop).

        V022 §5.3 notes the theoretical risk: if len(data) == 0 (empty
        short-read), cursor would not advance. In practice this cannot
        happen because PPSSPP returns at least 1 byte or raises. This
        test anchors that a 1-byte short-read still advances the cursor.
        """
        pattern = b"\xee"
        region_addr = 0x08804000
        # 8-byte region; handler returns 1 byte per call (extreme short-read)
        region_data = bytes([0x00] * 4 + [0xEE] + [0x00] * 3)
        scan_transport.set_response(
            "memory.read",
            _make_short_read_handler({region_addr: region_data}, 1),
        )

        matches = await scan_client.scan_memory(
            pattern=pattern,
            start=region_addr,
            end=region_addr + 8,
            chunk_size=4,
            max_results=10,
        )

        # Pattern at offset 4 must be found despite 1-byte short-reads
        assert len(matches) == 1
        assert matches[0]["address"] == region_addr + 4


# ============================================================================
# Overlap region: no duplicate matches (B.2 §5.3 algorithm)
# ============================================================================


class TestOverlapNoDuplicateMatches:
    """Overlap region handling: patterns in overlap are NOT double-counted.

    scan_memory reads chunk_size + overlap bytes per iteration, but only
    reports matches within [cursor, chunk_end). Matches in the overlap
    tail [chunk_end, chunk_end + overlap) are reported by the next chunk.

    L3 anchors that:
    1. A pattern at chunk_end boundary is reported exactly once
    2. A pattern spanning chunk_end is reported exactly once
    3. Overlap bytes are re-read but not double-matched
    """

    async def test_pattern_at_chunk_end_reported_once(self, scan_client, scan_transport):
        """Pattern exactly at chunk_end is reported by the next chunk.

        Pattern at offset = chunk_size (chunk_end of chunk 0) is in the
        overlap tail of chunk 0's read but at the start of chunk 1's
        read. It must be reported exactly once (by chunk 1).
        """
        pattern = b"\xab\xcd"
        region_addr = 0x08804000
        chunk_size = 16
        # Place pattern at offset 16 (= chunk_end of chunk 0)
        region_data = bytearray(b"\x00" * 32)
        region_data[16:18] = pattern
        scan_transport.set_response(
            "memory.read",
            _make_read_handler({region_addr: bytes(region_data)}),
        )

        matches = await scan_client.scan_memory(
            pattern=pattern,
            start=region_addr,
            end=region_addr + 32,
            chunk_size=chunk_size,
        )

        assert len(matches) == 1, (
            f"Pattern at chunk_end must be reported exactly once, got {len(matches)}: {matches}"
        )
        assert matches[0]["address"] == region_addr + 16

    async def test_pattern_spanning_chunk_end_reported_once(self, scan_client, scan_transport):
        """Pattern spanning chunk_end is caught by overlap, reported once.

        Pattern at offset chunk_size - 1 (1 byte in chunk 0, 1 byte in
        chunk 1) is found by chunk 0's overlap read. It must NOT be
        re-reported by chunk 1 (which re-reads the overlap bytes).
        """
        pattern = b"\xde\xad"
        region_addr = 0x08804000
        chunk_size = 16
        # Place pattern at offset 15 (1 byte in chunk 0, 1 byte in chunk 1)
        region_data = bytearray(b"\x00" * 32)
        region_data[15:17] = pattern
        scan_transport.set_response(
            "memory.read",
            _make_read_handler({region_addr: bytes(region_data)}),
        )

        matches = await scan_client.scan_memory(
            pattern=pattern,
            start=region_addr,
            end=region_addr + 32,
            chunk_size=chunk_size,
        )

        assert len(matches) == 1, (
            f"Spanning pattern must be reported exactly once (overlap "
            f"catches it, next chunk does not re-report). Got "
            f"{len(matches)}: {matches}"
        )
        assert matches[0]["address"] == region_addr + 15


# ============================================================================
# Unreadable region: cursor advances to chunk_end (no infinite loop)
# ============================================================================


class TestUnreadableRegionAdvance:
    """Unreadable region: cursor jumps to chunk_end, scan continues.

    When read_bytes raises, scan_memory sets `cursor = chunk_end` (NOT
    `cursor + len(data)`) and continues. This is distinct from the
    short-read path. L3 anchors that an unreadable region does NOT
    cause infinite loop and the scan continues at the next chunk.
    """

    async def test_unreadable_region_advances_to_chunk_end(self, scan_client, scan_transport):
        """Unreadable chunk: cursor = chunk_end, scan continues.

        Setup: chunk_size=16, first chunk raises, second chunk has pattern.
        After the first chunk raises, cursor must jump to chunk_end=16
        (not stay at 0), so the second read starts at 16.
        """
        pattern = b"\xff\xee"
        region_addr = 0x08804000
        chunk_size = 16
        # Second chunk has pattern at offset 4 (absolute offset 20)
        region_data = bytearray(b"\x00" * 32)
        region_data[20:22] = pattern

        def handler(**params):
            addr = params["address"]
            if addr < region_addr + chunk_size:
                raise RuntimeError("unmapped memory")
            # Second chunk: return data
            offset = addr - region_addr
            chunk = region_data[offset : offset + params["size"]]
            return {"base64": base64.b64encode(chunk).decode("ascii")}

        scan_transport.set_response("memory.read", handler)

        matches = await scan_client.scan_memory(
            pattern=pattern,
            start=region_addr,
            end=region_addr + 32,
            chunk_size=chunk_size,
        )

        assert len(matches) == 1
        assert matches[0]["address"] == region_addr + 20

    async def test_all_unreadable_returns_empty(self, scan_client, scan_transport):
        """All chunks unreadable: empty list, no infinite loop."""
        pattern = b"\xaa"

        def handler(**params):
            raise RuntimeError("unmapped")

        scan_transport.set_response("memory.read", handler)

        matches = await scan_client.scan_memory(
            pattern=pattern,
            start=0x08804000,
            end=0x08804000 + 64,
            chunk_size=16,
        )

        assert matches == []


# ============================================================================
# Multi-chunk scan: pattern found across chunks (algorithm integration)
# ============================================================================


class TestMultiChunkScanIntegration:
    """Multi-chunk scan integration: pattern found across multiple chunks.

    L3 anchors the end-to-end algorithm: multiple chunks read, overlap
    applied, matches collected, max_results respected. This is the
    integration of V022 §5.3's algorithm under realistic conditions.
    """

    async def test_pattern_in_each_chunk_all_found(self, scan_client, scan_transport):
        """Pattern in each of 4 chunks: all 4 matches returned."""
        pattern = b"\xbe\xef"
        region_addr = 0x08804000
        chunk_size = 16
        # 4 chunks × 16 bytes = 64 bytes; pattern at offset 4 of each chunk
        region_data = bytearray(b"\x00" * 64)
        for chunk_idx in range(4):
            region_data[chunk_idx * chunk_size + 4 : chunk_idx * chunk_size + 6] = pattern
        scan_transport.set_response(
            "memory.read",
            _make_read_handler({region_addr: bytes(region_data)}),
        )

        matches = await scan_client.scan_memory(
            pattern=pattern,
            start=region_addr,
            end=region_addr + 64,
            chunk_size=chunk_size,
        )

        assert len(matches) == 4
        expected_addrs = [
            region_addr + 4,
            region_addr + 20,
            region_addr + 36,
            region_addr + 52,
        ]
        assert [m["address"] for m in matches] == expected_addrs

    async def test_max_results_stops_mid_scan(self, scan_client, scan_transport):
        """max_results stops scan mid-way (no unnecessary reads).

        With 4 matches available and max_results=2, the scan must stop
        after finding 2 matches. L3 anchors that the scan terminates
        early (no infinite read loop after max_results reached).
        """
        pattern = b"\xaa"
        region_addr = 0x08804000
        # 256 bytes of 0xAA → 256 matches; max_results=2
        region_data = b"\xaa" * 256
        scan_transport.set_response(
            "memory.read",
            _make_read_handler({region_addr: region_data}),
        )

        matches = await scan_client.scan_memory(
            pattern=pattern,
            start=region_addr,
            end=region_addr + 256,
            chunk_size=64,
            max_results=2,
        )

        assert len(matches) == 2
        assert [m["address"] for m in matches] == [region_addr, region_addr + 1]
