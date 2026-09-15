"""L1 contract tests for memory read/write methods.

Anchors:
- memory.read_u8: MemorySubscriber.cpp:L36
- memory.read_u16: MemorySubscriber.cpp:L36
- memory.read_u32: MemorySubscriber.cpp:L36
- memory.read: MemorySubscriber.cpp:L37, L193-227 (base64 field)
- memory.readString: MemorySubscriber.cpp:L38, L240-269 (type param)
- memory.write_u8/u16/u32: MemorySubscriber.cpp:L39-41
- memory.write: MemorySubscriber.cpp:L42 (base64-encoded)
"""

from __future__ import annotations

import base64

import pytest


class TestMemoryReadContract:
    """L1 contract: memory read methods forward correct event + params."""

    @pytest.mark.asyncio
    async def test_read_u8_forwards_event_and_params(self, client, transport):
        """L1 anchor: read_u8 forwards `memory.read_u8` with address param.

        See MemorySubscriber.cpp:L36.
        """
        transport.set_response("memory.read_u8", {"value": 42})
        result = await client.read_u8(0x08804000)
        assert transport.calls[-1][0] == "memory.read_u8"
        assert transport.calls[-1][1] == {"address": 0x08804000}
        assert result == 42
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_read_u16_forwards_event_and_params(self, client, transport):
        """L1 anchor: read_u16 forwards `memory.read_u16` with address param.

        See MemorySubscriber.cpp:L36.
        """
        transport.set_response("memory.read_u16", {"value": 0x1234})
        result = await client.read_u16(0x08804000)
        assert transport.calls[-1][0] == "memory.read_u16"
        assert transport.calls[-1][1] == {"address": 0x08804000}
        assert result == 0x1234
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_read_u32_forwards_event_and_params(self, client, transport):
        """L1 anchor: read_u32 forwards `memory.read_u32` with address param.

        See MemorySubscriber.cpp:L36.
        """
        transport.set_response("memory.read_u32", {"value": 0xDEADBEEF})
        result = await client.read_u32(0x08804000)
        assert transport.calls[-1][0] == "memory.read_u32"
        assert transport.calls[-1][1] == {"address": 0x08804000}
        assert result == 0xDEADBEEF
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_read_u8_extracts_value_field(self, client, transport):
        """L1 anchor: read_u8 returns the `value` field as int (default 0).

        See MemorySubscriber.cpp:L36 — response field is `value`.
        """
        transport.set_response("memory.read_u8", {"value": 255})
        result = await client.read_u8(0x08804000)
        assert result == 255

    @pytest.mark.asyncio
    async def test_read_bytes_forwards_event_and_params(self, client, transport):
        """L1 anchor: read_bytes forwards `memory.read` with address+size.

        See MemorySubscriber.cpp:L37, L193-227.
        """
        encoded = base64.b64encode(b"hello").decode("ascii")
        transport.set_response("memory.read", {"base64": encoded})
        result = await client.read_bytes(0x08804000, 5)
        assert transport.calls[-1][0] == "memory.read"
        assert transport.calls[-1][1] == {"address": 0x08804000, "size": 5}
        assert result == b"hello"
        assert isinstance(result, bytes)

    @pytest.mark.asyncio
    async def test_read_bytes_decodes_base64_field(self, client, transport):
        """L1 anchor: read_bytes decodes the `base64` field (not `data`).

        See MemorySubscriber.cpp:L37, L193-227 — response field is `base64`.
        """
        payload = bytes(range(256))
        encoded = base64.b64encode(payload).decode("ascii")
        transport.set_response("memory.read", {"base64": encoded})
        result = await client.read_bytes(0x08804000, len(payload))
        assert result == payload

    @pytest.mark.asyncio
    async def test_read_string_uses_bounded_read_with_nul_scan(self, client, transport):
        """L1 anchor (F-3 fix): read_string uses bounded memory.read +
        local NUL scan — PPSSPP memory.readString is never called (its
        strnlen scans to memory end and a giant response can kill the
        WebSocket).

        The bounded read forwards address + the default 4096-byte cap.
        """
        payload = "你好".encode("utf-8") + b"\x00" + b"trailing"
        import base64 as _b64

        transport.set_response(
            "memory.read",
            {"base64": _b64.b64encode(payload).decode("ascii")},
        )
        result = await client.read_string(0x08804000)
        assert transport.calls[-1][0] == "memory.read"
        assert transport.calls[-1][1] == {
            "address": 0x08804000,
            "size": 4096,
        }
        assert result == "你好"
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_read_string_stops_at_nul_and_honors_max_length(self, client, transport):
        """L1 anchor (F-3 fix): content after the first NUL is dropped and
        an explicit max_length caps the read size."""
        import base64 as _b64

        transport.set_response(
            "memory.read",
            {"base64": _b64.b64encode(b"abc\x00xyz").decode("ascii")},
        )
        result = await client.read_string(0x08804000, max_length=64)
        assert result == "abc"
        assert transport.calls[-1][1] == {"address": 0x08804000, "size": 64}

    @pytest.mark.asyncio
    async def test_read_string_honors_64k_cap(self, client, transport):
        """W1 fix (2026-09-06): the client honors max_length up to 65536 —
        the previous hard 4096 re-clamp silently truncated the tool layer's
        advertised 64 KiB max_len."""
        import base64 as _b64

        transport.set_response(
            "memory.read",
            {"base64": _b64.b64encode(b"x" * 65536).decode("ascii")},
        )
        result = await client.read_string(0x08804000, max_length=65536)
        assert transport.calls[-1][1] == {"address": 0x08804000, "size": 65536}
        assert len(result) == 65536


class TestMemoryWriteContract:
    """L1 contract: memory write methods forward correct event + params."""

    @pytest.mark.asyncio
    async def test_write_u8_forwards_event_and_params(self, client, transport):
        """L1 anchor: write_u8 forwards `memory.write_u8` with address+value.

        See MemorySubscriber.cpp:L39.
        """
        await client.write_u8(0x08804000, 0xAB)
        assert transport.calls[-1][0] == "memory.write_u8"
        assert transport.calls[-1][1] == {
            "address": 0x08804000,
            "value": 0xAB,
        }

    @pytest.mark.asyncio
    async def test_write_u16_forwards_event_and_params(self, client, transport):
        """L1 anchor: write_u16 forwards `memory.write_u16` with address+value.

        See MemorySubscriber.cpp:L40.
        """
        await client.write_u16(0x08804000, 0xBEEF)
        assert transport.calls[-1][0] == "memory.write_u16"
        assert transport.calls[-1][1] == {
            "address": 0x08804000,
            "value": 0xBEEF,
        }

    @pytest.mark.asyncio
    async def test_write_u32_forwards_event_and_params(self, client, transport):
        """L1 anchor: write_u32 forwards `memory.write_u32` with address+value.

        See MemorySubscriber.cpp:L41.
        """
        await client.write_u32(0x08804000, 0xDEADBEEF)
        assert transport.calls[-1][0] == "memory.write_u32"
        assert transport.calls[-1][1] == {
            "address": 0x08804000,
            "value": 0xDEADBEEF,
        }

    @pytest.mark.asyncio
    async def test_write_bytes_forwards_event_and_base64(self, client, transport):
        """L1 anchor: write_bytes forwards `memory.write` with address+base64.

        See MemorySubscriber.cpp:L42 — payload is base64-encoded and sent
        under the `base64` param name.
        """
        payload = b"\x00\x01\x02\xFF"
        expected_b64 = base64.b64encode(payload).decode("ascii")
        await client.write_bytes(0x08804000, payload)
        assert transport.calls[-1][0] == "memory.write"
        assert transport.calls[-1][1] == {
            "address": 0x08804000,
            "base64": expected_b64,
        }
        # Sanity: the encoded payload round-trips.
        assert base64.b64decode(transport.calls[-1][1]["base64"]) == payload
