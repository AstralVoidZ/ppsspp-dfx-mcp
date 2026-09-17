"""Tests for the FakeTransport write overlay (blind-eval finding W2).

Run (from mcps/ppsspp-dfx-mcp/):
  ../.venv/ppsspp-dfx-mcp/Scripts/python.exe -m pytest tests/fake_transport/test_write_overlay.py -q
"""

from __future__ import annotations

import base64

import pytest

from fake_transport import FakeTransport


@pytest.mark.asyncio
async def test_write_u32_then_read_u32_roundtrip():
    t = FakeTransport()
    await t.call("memory.write_u32", address=0x09000000, value=0x11223344)
    resp = await t.call("memory.read_u32", address=0x09000000)
    assert resp == {"value": 0x11223344}


@pytest.mark.asyncio
async def test_write_u8_u16_partial_overlap():
    t = FakeTransport()
    await t.call("memory.write_u8", address=0x1000, value=0xAB)
    await t.call("memory.write_u16", address=0x1001, value=0xCDEF)
    resp = await t.call("memory.read", address=0x1000, size=4)
    raw = base64.b64decode(resp["base64"])
    assert raw == bytes([0xAB, 0xEF, 0xCD, 0x00])  # little-endian, unwritten → 0


@pytest.mark.asyncio
async def test_write_bytes_base64_roundtrip():
    t = FakeTransport()
    payload = bytes([1, 2, 3, 4, 5])
    await t.call("memory.write", address=0x2000, base64=base64.b64encode(payload).decode("ascii"))
    resp = await t.call("memory.read", address=0x2000, size=5)
    assert base64.b64decode(resp["base64"]) == payload


@pytest.mark.asyncio
async def test_read_without_overlap_falls_through():
    t = FakeTransport()
    t.set_response("memory.read_u32", {"value": 666763200})
    await t.call("memory.write_u32", address=0x9000, value=1)
    # untouched address → fixture response still applies
    resp = await t.call("memory.read_u32", address=0x8000)
    assert resp == {"value": 666763200}


@pytest.mark.asyncio
async def test_partial_overlap_wins_over_fixture():
    t = FakeTransport()
    t.set_response("memory.read_u32", {"value": 666763200})
    await t.call("memory.write_u8", address=0x8002, value=0xFF)
    resp = await t.call("memory.read_u32", address=0x8000)
    # overlap → synthesized: bytes 00 00 FF 00 little-endian
    assert resp == {"value": 0x00FF0000}


@pytest.mark.asyncio
async def test_zero_size_read_returns_none_path():
    t = FakeTransport()
    await t.call("memory.write_u32", address=0x9000, value=1)
    resp = await t.call("memory.read", address=0x9000, size=0)
    assert resp == {}  # no overlay synthesis, no fixture → {}
