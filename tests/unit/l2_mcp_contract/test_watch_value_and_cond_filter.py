"""Tests for the D2 value-change watch tool and the D1 condition filter registry."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.core import cond_filter
from ppsspp_dfx_mcp.tools import watch_value as wv_mod

# ---------------------------------------------------------------------
# cond_filter registry (🔴-1/D1 enforcement state)
# ---------------------------------------------------------------------


def test_cond_filter_register_get_drop():
    cond_filter.register("s1", 0x1000, "s1==0x711")
    entry = cond_filter.get("s1", 0x1000)
    assert entry is not None and entry["condition"] == "s1==0x711"
    assert entry["filtered"] == 0 and entry["hits"] == 0
    cond_filter.drop("s1", 0x1000)
    assert cond_filter.get("s1", 0x1000) is None


def test_cond_filter_bump_counters():
    cond_filter.register("s2", 0x2000, "a0==1")
    assert cond_filter.bump_hit("s2", 0x2000) == 1
    assert cond_filter.bump_filtered("s2", 0x2000) == 1
    assert cond_filter.bump_filtered("s2", 0x2000) == 2
    entry = cond_filter.get("s2", 0x2000)
    assert entry["hits"] == 1 and entry["filtered"] == 2
    cond_filter.drop("s2", 0x2000)


def test_cond_filter_drop_session():
    cond_filter.register("sx", 0x1, "a0==1")
    cond_filter.register("sx", 0x2, "a0==2")
    cond_filter.register("sy", 0x3, "a0==3")
    n = cond_filter.drop_session("sx")
    assert n == 2
    assert cond_filter.get("sx", 0x1) is None
    assert cond_filter.get("sy", 0x3) is not None
    cond_filter.drop_session("sy")


# ---------------------------------------------------------------------
# watch_value (D2 storm-free polling watch)
# ---------------------------------------------------------------------


def _patch_watch(monkeypatch: pytest.MonkeyPatch, values: list[bytes]):
    mock_client = AsyncMock()

    async def fake_read(address: int, size: int):
        # pop through the scripted values, holding the last one
        nonlocal _i
        v = values[min(_i, len(values) - 1)]
        _i += 1
        return v

    _i = 0
    mock_client.read_bytes.side_effect = fake_read

    @asynccontextmanager
    async def fake_session_client(session_id: str):
        yield mock_client

    async def fake_alive(session_id: str):
        return None

    monkeypatch.setattr(wv_mod, "session_client", fake_session_client)
    monkeypatch.setattr(wv_mod, "validate_session_alive", fake_alive)
    return mock_client


@pytest.mark.asyncio
async def test_watch_value_records_changes(monkeypatch: pytest.MonkeyPatch):
    # values (u32 LE): 0x111 → 0x222 → 0x222 (no change) → 0x333
    seq = [
        (0x111).to_bytes(4, "little"),
        (0x222).to_bytes(4, "little"),
        (0x222).to_bytes(4, "little"),
        (0x333).to_bytes(4, "little"),
    ]

    mock = AsyncMock()

    async def fake_read(address: int, size: int):
        nonlocal _i
        v = seq[min(_i, len(seq) - 1)]
        _i += 1
        return v

    _i = 0
    mock.read_bytes.side_effect = fake_read

    @asynccontextmanager
    async def fake_session_client(session_id: str):
        yield mock

    async def fake_alive(session_id: str):
        return None

    monkeypatch.setattr(wv_mod, "session_client", fake_session_client)
    monkeypatch.setattr(wv_mod, "validate_session_alive", fake_alive)
    monkeypatch.setattr(wv_mod, "interval_frames", 1, raising=False)

    # 直接驱动内部循环：monkeypatch asyncio.sleep 消除等待
    real_sleep = __import__("asyncio").sleep

    async def fast_sleep(_):
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", fast_sleep)

    # duration 4 个采样点
    result = await wv_mod.watch_value(
        session_id="s",
        address="0x08A0D000",
        mode="u32",
        interval_frames=1,
        duration_frames=4,
    )
    out = result
    assert out["samples"] >= 3
    assert out["first_value"] == 0x111
    assert out["changes"][0]["old"] == 0x111 and out["changes"][0]["new"] == 0x222
    assert out["change_count"] >= 1


@pytest.mark.asyncio
async def test_watch_value_rejects_bad_args():
    from ppsspp_dfx_mcp.errors import ArgsInvalid

    with pytest.raises(ArgsInvalid):
        await wv_mod.watch_value(
            session_id="s",
            address="0x08A0D000",
            duration_frames=20000,
        )
