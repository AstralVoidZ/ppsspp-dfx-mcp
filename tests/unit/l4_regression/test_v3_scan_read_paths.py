"""W6 / W10 / W11 (review v3): scan read paths.

- W6: a short read on the narrow path used to raise struct.error, which
  turned the whole narrow batch into [INTERNAL]; the initial path already
  had a length guard.
- W10: the value-scan inner loop unpacked every element in Python on the
  event loop (1 MiB u16 ≈ 0.168 s measured); the eq fast path must return
  exactly the same offsets as the naive loop and keep the hit cap.
- W11: `_read_segments` aggregated the whole range in memory; it must
  stream one chunk at a time.
"""

from __future__ import annotations

import inspect
import random

import pytest

from ppsspp_dfx_mcp.core.primitives import MAX_SINGLE_READ_BYTES
from ppsspp_dfx_mcp.tools import scan as scan_mod

_FMT = {1: "B", 2: "H", 4: "I"}


class _StubClient:
    """Client whose read_bytes is a caller-supplied function."""

    def __init__(self, read_fn) -> None:
        self._read_fn = read_fn
        self.calls: list[tuple[int, int]] = []

    async def read_bytes(self, address: int, size: int) -> bytes:
        self.calls.append((address, size))
        return self._read_fn(address, size)


class _StubSessionClient:
    def __init__(self, client: _StubClient) -> None:
        self._client = client

    async def __aenter__(self) -> _StubClient:
        return self._client

    async def __aexit__(self, *exc) -> None:
        return None


def _patch_client(monkeypatch: pytest.MonkeyPatch, read_fn) -> _StubClient:
    client = _StubClient(read_fn)
    monkeypatch.setattr(scan_mod, "session_client", lambda session_id: _StubSessionClient(client))
    return client


# ---------------------------------------------------------------------
# W6 — short reads must be skipped, not raise
# ---------------------------------------------------------------------


async def test_narrow_short_merged_read_skips_candidates(monkeypatch: pytest.MonkeyPatch):
    # Merged run (0x1000..0x1008) comes back 2 bytes short.
    _patch_client(monkeypatch, lambda address, size: b"\x01\x02")
    out = await scan_mod._narrow_candidates("s", [0x1000, 0x1004], 0, 4, "eq")
    # Pre-fix: struct.error propagates out of _narrow_candidates.
    assert out == []


async def test_narrow_short_point_read_skips_candidate(monkeypatch: pytest.MonkeyPatch):
    def read_fn(address: int, size: int) -> bytes:
        if size > 4:
            raise RuntimeError("merged read fails")
        return b"\x01\x02"  # short for a 4-byte width

    _patch_client(monkeypatch, read_fn)
    out = await scan_mod._narrow_candidates("s", [0x1000, 0x1004], 0, 4, "eq")
    # Pre-fix: the per-address fallback also struct.error'd.
    assert out == []


async def test_narrow_full_reads_still_match(monkeypatch: pytest.MonkeyPatch):
    payload = (5).to_bytes(4, "little") + (9).to_bytes(4, "little")

    def read_fn(address: int, size: int) -> bytes:
        return payload[:size]

    _patch_client(monkeypatch, read_fn)
    out = await scan_mod._narrow_candidates("s", [0x1000, 0x1004], 5, 4, "eq")
    assert out == [0x1000]


# ---------------------------------------------------------------------
# W10 — eq fast path == naive loop, hit cap preserved
# ---------------------------------------------------------------------


def _naive_hits(data: bytes, size: int, op: str, value: int, limit: int) -> list[int]:
    out: list[int] = []
    for off in range(0, len(data) - size + 1):
        v = int.from_bytes(data[off : off + size], "little")
        if scan_mod._cmp(v, op, value):
            out.append(off)
            if len(out) >= limit:
                break
    return out


@pytest.mark.parametrize("op", ["eq", "ne"])
def test_block_hits_match_naive_on_random_data(op: str):
    rnd = random.Random(20260930)
    data = bytes(rnd.randrange(256) for _ in range(256 * 1024))
    # Sprinkle a repeating pattern so eq has something to find.
    data = (b"\x2a\x00\x2a\x00" * 512) + data
    size = rnd.choice([1, 2, 4])
    value = rnd.randrange(1 << (8 * size))
    assert scan_mod._scan_block_hits(data, _FMT[size], op, value, 10**9) == _naive_hits(
        data, size, op, value, 10**9
    )


@pytest.mark.parametrize("op", ["eq", "ne", "lt", "gt"])
def test_block_hits_match_naive_on_dense_values(op: str):
    rnd = random.Random(7)
    data = bytes(rnd.choice([0, 1, 2, 3]) for _ in range(64 * 1024))
    assert scan_mod._scan_block_hits(data, "B", op, 2, 10**9) == _naive_hits(data, 1, op, 2, 10**9)


def test_block_hits_respects_limit_and_short_blob():
    data = b"\x01\x01\x01\x01"
    assert scan_mod._scan_block_hits(data, "B", "eq", 1, 2) == [0, 1]
    assert scan_mod._scan_block_hits(b"\x01", "I", "eq", 1, 10) == []
    assert scan_mod._scan_block_hits(data, "B", "eq", 0x100, 10) == []  # out of width


def test_block_hits_eq_finds_overlapping_matches():
    # naive per-offset scan includes overlapping matches.
    assert scan_mod._scan_block_hits(b"\xaa\xaa\xaa", "B", "eq", 0xAA, 10**9) == [0, 1, 2]
    assert scan_mod._scan_block_hits(b"\xaa\xaa", "H", "eq", 0xAAAA, 10**9) == [0]


async def test_initial_scan_candidates_match_naive(monkeypatch: pytest.MonkeyPatch):
    """End-to-end: the initial value scan returns the naive candidate set."""
    rnd = random.Random(99)
    data = bytes(rnd.choice([0, 1, 2]) for _ in range(8 * 1024))
    base = 0x08804000

    def read_fn(address: int, size: int) -> bytes:
        off = address - base
        return data[off : off + size]

    _patch_client(monkeypatch, read_fn)
    scan_mod._reset_value_sessions_for_tests()
    out = await scan_mod._scan_value(
        "s", "initial", 1, "u8", "eq", None, hex(base), hex(base + len(data))
    )
    expected = [base + off for off in _naive_hits(data, 1, "eq", 1, scan_mod._VALUE_MAX_HITS)]
    assert out.candidates == len(expected)
    assert scan_mod._VALUE_SESSIONS[out.scan_handle]["addresses"] == expected


# ---------------------------------------------------------------------
# W11 — segment reads must stream
# ---------------------------------------------------------------------


async def test_iter_segments_is_lazy_async_generator(monkeypatch: pytest.MonkeyPatch):
    client = _patch_client(monkeypatch, lambda address, size: bytes(size))
    span = 3 * MAX_SINGLE_READ_BYTES
    agen = scan_mod._iter_segments(client, 0x1000, span)
    assert inspect.isasyncgen(agen)
    first = await agen.__anext__()
    assert first[0] == 0x1000 and len(first[1]) == MAX_SINGLE_READ_BYTES
    # Still only one chunk fetched — the generator does not pre-read.
    assert len(client.calls) == 1
    second = await agen.__anext__()
    assert second[0] == 0x1000 + MAX_SINGLE_READ_BYTES
    assert len(client.calls) == 2
    third = await agen.__anext__()
    assert third[0] == 0x1000 + 2 * MAX_SINGLE_READ_BYTES
    assert len(client.calls) == 3
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()


async def test_iter_segments_skips_unreadable_chunks(monkeypatch: pytest.MonkeyPatch):
    def read_fn(address: int, size: int) -> bytes:
        if address != 0x1000:
            raise RuntimeError("unmapped")
        return b"a" * size

    client = _patch_client(monkeypatch, read_fn)
    segs = [seg async for seg in scan_mod._iter_segments(client, 0x1000, 2 * MAX_SINGLE_READ_BYTES)]
    assert segs == [(0x1000, b"a" * MAX_SINGLE_READ_BYTES)]
