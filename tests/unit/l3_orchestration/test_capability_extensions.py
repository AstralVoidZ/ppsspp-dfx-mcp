"""L3 orchestration tests: capability extensions (source-driven additions).

Anchors (PPSSPP source, cross-checked 2026-09-06):
- write_memory u8/u16: MemorySubscriber.cpp:39/40-41 (memory.write_u8 /
  memory.write_u16) — byte/halfword granules for patch workflows. Value
  range is validated client-side so PPSSPP's u32 parser never sees an
  out-of-range granule.
- query(register): CPUCoreSubscriber.cpp:269-323 (cpu.getReg name mode) —
  single-register query avoids the full getAllRegs payload (three parallel
  arrays per category).
- dump_clut: GPUBufferSubscriber.cpp:406-427 (gpu.buffer.clut) — palette
  dump for 2D tile / font-glyph debugging.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.errors import ToolError
from ppsspp_dfx_mcp.tools.memory import write_memory
from ppsspp_dfx_mcp.tools.query import query


def _mock_session_client(tool_module: str) -> AsyncMock:
    mock_client = AsyncMock()

    @asynccontextmanager
    async def _cm(session_id: str) -> AsyncIterator[AsyncMock]:
        yield mock_client

    return mock_client


# ============================================================================
# write_memory: u8 / u16 granule formats
# ============================================================================


class TestWriteMemoryGranules:
    """u8/u16 formats forward the matching WS event with range validation."""

    @pytest.mark.asyncio
    async def test_u8_forwards_write_u8(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_client = AsyncMock()

        @asynccontextmanager
        async def _cm(session_id: str) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client", _cm
        )

        await write_memory(
            session_id="sess-1", address="0x08804000",
            data="0xAB", format="u8", force=True,
        )

        mock_client.write_u8.assert_awaited_once_with(
            address=0x08804000, value=0xAB
        )
        mock_client.write_u32.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_u16_forwards_write_u16(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_client = AsyncMock()

        @asynccontextmanager
        async def _cm(session_id: str) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client", _cm
        )

        await write_memory(
            session_id="sess-1", address="0x08804000",
            data="0xBEEF", format="u16", force=True,
        )

        mock_client.write_u16.assert_awaited_once_with(
            address=0x08804000, value=0xBEEF
        )

    @pytest.mark.asyncio
    async def test_u8_rejects_out_of_range_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0x1FF does not fit a byte — fail client-side with a precise
        message instead of letting PPSSPP truncate or mis-parse."""
        mock_client = AsyncMock()

        @asynccontextmanager
        async def _cm(session_id: str) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client", _cm
        )

        with pytest.raises(ToolError, match="out of range for format='u8'"):
            await write_memory(
                session_id="sess-1", address="0x08804000",
                data="0x1FF", format="u8", force=True,
            )
        mock_client.write_u8.assert_not_awaited()


# ============================================================================
# query: register action (single-register query)
# ============================================================================


class TestQueryRegisterAction:
    """query(action='register') resolves one register by name."""

    @pytest.mark.asyncio
    async def test_forwards_name_and_returns_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client = AsyncMock()
        mock_client.get_reg.return_value = {
            "category": 0, "register": 4, "uintValue": 42, "floatValue": "0.0",
        }

        @asynccontextmanager
        async def _cm(session_id: str) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.query.session_client", _cm
        )

        result = await query(
            session_id="sess-1", action="register", name="a0",
        )

        mock_client.get_reg.assert_awaited_once_with(name="a0", thread=None)
        assert result["data"]["uintValue"] == 42

    @pytest.mark.asyncio
    async def test_missing_name_fails_before_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client = AsyncMock()

        @asynccontextmanager
        async def _cm(session_id: str) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.query.session_client", _cm
        )

        with pytest.raises(ToolError, match="requires a register name"):
            await query(session_id="sess-1", action="register")
        mock_client.get_reg.assert_not_awaited()
