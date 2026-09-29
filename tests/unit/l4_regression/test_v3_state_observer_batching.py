"""W12 (review v3): state_observer must not do N+1 WS round-trips.

`_observe_probes` read every probe with its own `memory.read_uN` call for
every sample (50 probes x 1400 samples ≈ 70k round-trips). Adjacent probes
must be folded into one block read per sample, with a fallback to the
per-point reads when the block read fails.
"""

from __future__ import annotations

from ppsspp_dfx_mcp.models.state_observer import StateProbe
from ppsspp_dfx_mcp.tools import state_observer as so_mod

BASE = 0x08A0D000


class _StubClient:
    """Counts every WS round-trip; serves reads out of a flat buffer."""

    def __init__(self, buf: bytearray, block_fails: bool = False) -> None:
        self._buf = buf
        self._block_fails = block_fails
        self.round_trips = 0

    async def read_bytes(self, address: int, size: int) -> bytes:
        self.round_trips += 1
        if self._block_fails:
            raise RuntimeError("block read failed")
        off = address - BASE
        return bytes(self._buf[off : off + size])

    async def read_u8(self, address: int) -> int:
        self.round_trips += 1
        return self._buf[address - BASE]

    async def read_u16(self, address: int) -> int:
        self.round_trips += 1
        off = address - BASE
        return int.from_bytes(self._buf[off : off + 2], "little")

    async def read_u32(self, address: int) -> int:
        self.round_trips += 1
        off = address - BASE
        return int.from_bytes(self._buf[off : off + 4], "little")


def _probe(name: str, offset: int, size: int = 4) -> StateProbe:
    return StateProbe(name=name, address=BASE + offset, size=size)


async def test_adjacent_probes_fold_into_block_reads():
    buf = bytearray(0x40)
    buf[0:4] = (0x11111111).to_bytes(4, "little")
    buf[4:8] = (0x22222222).to_bytes(4, "little")
    buf[8:12] = (0x33333333).to_bytes(4, "little")
    client = _StubClient(buf)
    probes = (_probe("a", 0), _probe("b", 4), _probe("c", 8))

    result = await so_mod._observe_probes(client, probes, 2)

    # Pre-fix: 3 probes x 2 samples = 6 per-point reads (3 probes x 2 samples).
    assert client.round_trips <= 4
    assert [o.value for o in result.observations] == [0x11111111, 0x22222222, 0x33333333]
    assert [o.name for o in result.observations] == ["a", "b", "c"]
    assert result.success_count == 3 and result.failure_count == 0


async def test_block_read_failure_falls_back_to_point_reads():
    buf = bytearray(0x40)
    buf[0:4] = (0xAAAAAAAA).to_bytes(4, "little")
    buf[4:8] = (0xBBBBBBBB).to_bytes(4, "little")
    client = _StubClient(buf, block_fails=True)
    probes = (_probe("a", 0), _probe("b", 4))

    result = await so_mod._observe_probes(client, probes, 1)

    assert [o.value for o in result.observations] == [0xAAAAAAAA, 0xBBBBBBBB]
    assert result.failure_count == 0


async def test_mixed_width_adjacent_probes_keep_values():
    buf = bytearray(0x40)
    buf[0] = 0x41
    buf[2:4] = (0x0203).to_bytes(2, "little")
    buf[4:8] = (0x04050607).to_bytes(4, "little")
    client = _StubClient(buf)
    probes = (_probe("u8", 0, 1), _probe("u16", 2, 2), _probe("u32", 4, 4))

    result = await so_mod._observe_probes(client, probes, 1)

    assert [o.value for o in result.observations] == [0x41, 0x0203, 0x04050607]


async def test_distant_probes_still_read_per_run():
    buf = bytearray(0x2000)
    buf[0:4] = (1).to_bytes(4, "little")
    buf[0x1000 : 0x1000 + 4] = (2).to_bytes(4, "little")
    client = _StubClient(buf)
    probes = (_probe("near", 0), _probe("far", 0x1000))

    result = await so_mod._observe_probes(client, probes, 1)

    assert [o.value for o in result.observations] == [1, 2]


async def test_failing_probe_still_records_last_good_value():
    buf = bytearray(0x40)
    buf[0:4] = (7).to_bytes(4, "little")
    client = _StubClient(buf)

    calls = {"n": 0}

    async def flaky(address: int) -> int:
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("read failed")
        return int.from_bytes(buf[address - BASE : address - BASE + 4], "little")

    client.read_u32 = flaky  # single member -> point-read path
    probes = (_probe("solo", 0),)
    result = await so_mod._observe_probes(client, probes, 2)

    assert result.observations[0].error != ""
    assert result.observations[0].value == 7  # first sample succeeded, second failed
    assert result.failure_count == 1
