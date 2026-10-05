"""W2 (review v3): ppsspp_watch_value must stay inside its frame budget.

`interval_frames` only had a lower bound and the inner sleep loop ignored
the frames already consumed, so `interval_frames=10**6` slept ~4.6 h while
holding the session lock — even though the tool advertises a 18000-frame
cap. These tests pin both the rejection of an out-of-budget interval and
the "total sleeps <= duration_frames" invariant.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.tools import watch_value as wv_mod


def _patch_client(monkeypatch: pytest.MonkeyPatch) -> None:
    mock = AsyncMock()

    async def fake_read(address: int, size: int) -> bytes:
        return (7).to_bytes(size, "little")

    mock.read_bytes.side_effect = fake_read

    @asynccontextmanager
    async def fake_session_client(session_id: str):
        yield mock

    async def fake_alive(session_id: str) -> None:
        return None

    monkeypatch.setattr(wv_mod, "session_client", fake_session_client)
    monkeypatch.setattr(wv_mod, "validate_session_alive", fake_alive)


def _count_sleeps(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replace asyncio.sleep with an instant, non-yielding counter.

    The polling loop's only await in the inner loop is asyncio.sleep, so
    counting it counts frames waited — without burning wall clock.
    """
    calls = {"n": 0}

    async def instant_sleep(_delay: float) -> None:
        calls["n"] += 1

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)
    return calls


async def test_interval_exceeding_duration_is_rejected(monkeypatch: pytest.MonkeyPatch):
    _patch_client(monkeypatch)
    _count_sleeps(monkeypatch)
    with pytest.raises(ArgsInvalid, match="interval_frames"):
        await wv_mod.watch_value(
            session_id="s",
            address="0x08A0D000",
            interval_frames=10**6,
            duration_frames=600,
        )


async def test_total_sleeps_bounded_by_duration_frames(monkeypatch: pytest.MonkeyPatch):
    _patch_client(monkeypatch)
    calls = _count_sleeps(monkeypatch)
    await wv_mod.watch_value(
        session_id="s",
        address="0x08A0D000",
        mode="u32",
        interval_frames=8,
        duration_frames=10,
    )
    # Pre-fix: 8 (sample 1) + 8 (sample 2) = 16 sleeps for a 10-frame window.
    assert calls["n"] <= 10


@pytest.mark.asyncio
async def test_short_read_is_rejected_not_misread(monkeypatch: pytest.MonkeyPatch):
    """Review-v4 W-3: a short read must fail loudly, not be misread.

    read_bytes can legally return fewer bytes than requested (mapping tail,
    malformed-but-successful response). `int.from_bytes(raw[:size])` then
    interprets the shorter payload at the wrong width — a u32 watch
    silently reports a u16 value and the change timeline is built on bad
    data. Sibling readers (probe_observer, scan narrow) already guard
    len(data) == size; the watch loop is the unguarded site.
    """
    mock = AsyncMock()

    async def fake_read(address: int, size: int) -> bytes:
        return (7).to_bytes(max(1, size - 2), "little")  # u32 read → 2 bytes

    mock.read_bytes.side_effect = fake_read

    @asynccontextmanager
    async def fake_session_client(session_id: str):
        yield mock

    async def fake_alive(session_id: str) -> None:
        return None

    monkeypatch.setattr(wv_mod, "session_client", fake_session_client)
    monkeypatch.setattr(wv_mod, "validate_session_alive", fake_alive)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    with pytest.raises(ArgsInvalid, match="short read"):
        await wv_mod.watch_value(
            session_id="sess-1",
            address="0x08804000",
            mode="u32",
            duration_frames=10,
            interval_frames=1,
        )


async def _noop_sleep(_delay: float) -> None:
    return None
