"""L3 orchestration tests: output governance (batch 3).

Anchors:
- D-27: gpu_record raw strips 'uri' field (contains base64 payload)
- D-20: disassemble count capped at 100 + instruction field simplification
- D-08/D-14: funcs/func_scan top_n truncation
- D-10/D-30: search_disasm loop search collects multiple matches

L3 focus (tool wrapper orchestration, NOT WS forwarding):
- gpu_record: response.raw must not contain 'uri', 'base64', or 'data'
- disassemble: count=1000 capped to 100; extra fields stripped per instruction
- query(funcs, top_n=5): data list truncated to 5 entries
- search_disasm: loop calls search_disasm multiple times, collects up to max_results
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.tools.memory import disassemble
from ppsspp_dfx_mcp.tools.query import query
from ppsspp_dfx_mcp.tools.search_disasm import search_disasm
from ppsspp_dfx_mcp.views.gpu_record import GpuRecordResponse
from ppsspp_dfx_mcp.models.gpu_record import GpuRecordResult


# ============================================================================
# D-27: gpu_record raw strips 'uri' field
# ============================================================================


class TestGpuRecordRawStripsUri:
    """L3: GpuRecordResponse.from_result strips 'uri' from raw (D-27).

    PPSSPP returns the dump as a data URI in the 'uri' field
    (e.g. 'data:application/octet-stream;base64,...') which contains
    the full base64-encoded payload. The view must strip it along
    with 'base64' and 'data' to avoid embedding large binary data
    in the JSON response.
    """

    def test_uri_stripped_from_raw(self) -> None:
        """raw must not contain 'uri' key."""
        raw = {
            "uri": "data:application/octet-stream;base64,AAAA",
            "base64": "AAAA",
            "data": "AAAA",
            "frame": 123,
        }
        result = GpuRecordResult.from_raw(raw)
        response = GpuRecordResponse.from_result(result, file_path="/tmp/test.dump")

        assert "uri" not in response.raw, (
            "D-27: 'uri' field must be stripped from raw — it contains "
            "the full base64 payload (data URI), causing 386KB+ responses."
        )
        assert "base64" not in response.raw
        assert "data" not in response.raw
        # Metadata fields are preserved.
        assert response.raw.get("frame") == 123

    def test_size_bytes_extracted_from_uri(self) -> None:
        """Even with uri stripped, size_bytes reflects decoded data."""
        raw = {
            "uri": "data:application/octet-stream;base64,AAAA",
        }
        result = GpuRecordResult.from_raw(raw)
        response = GpuRecordResponse.from_result(result, file_path="/tmp/test.dump")

        assert response.size_bytes == 3  # b64decode("AAAA") = 3 bytes
        assert "uri" not in response.raw


# ============================================================================
# D-20: disassemble count cap + field simplification
# ============================================================================


class TestDisassembleCountCap:
    """L3: disassemble caps count at 100 (D-20).

    PPSSPP's memory.disasm returns one dict per instruction; count=1000
    produces ~645KB responses. The tool caps count to 100.
    """

    @pytest.mark.asyncio
    async def test_count_above_100_capped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """count=1000 forwarded to client.disasm as count=100."""
        mock_client = AsyncMock()
        mock_client.disasm.return_value = [{"text": "nop", "address": 0x08804000}]

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        result = await disassemble(
            session_id="sess-1", address=0x08804000, count=1000
        )

        # Verify count was capped to 100.
        call_kwargs = mock_client.disasm.call_args.kwargs
        assert call_kwargs["count"] == 100, (
            "D-20: count=1000 must be capped to 100 before calling "
            "client.disasm — prevents 645KB+ responses."
        )

    @pytest.mark.asyncio
    async def test_count_below_100_not_capped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """count=10 forwarded as-is (below cap)."""
        mock_client = AsyncMock()
        mock_client.disasm.return_value = [{"text": "nop", "address": 0x08804000}]

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        await disassemble(
            session_id="sess-1", address=0x08804000, count=10
        )

        call_kwargs = mock_client.disasm.call_args.kwargs
        assert call_kwargs["count"] == 10

    @pytest.mark.asyncio
    async def test_count_exactly_100_not_capped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """count=100 forwarded as-is (exact boundary, not capped to 99)."""
        mock_client = AsyncMock()
        mock_client.disasm.return_value = [{"text": "nop", "address": 0x08804000}]

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        await disassemble(
            session_id="sess-1", address=0x08804000, count=100
        )

        call_kwargs = mock_client.disasm.call_args.kwargs
        assert call_kwargs["count"] == 100


class TestDisassembleFieldSimplification:
    """L3: disassemble strips extra fields per instruction (D-20).

    PPSSPP's memory.disasm may return extra fields (encoding,
    branchDelay, isBranch, etc.) that bloat the response. The tool
    keeps only address/text/name/params.
    """

    @pytest.mark.asyncio
    async def test_extra_fields_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """encoding/branchDelay/isBranch stripped, address/text kept."""
        mock_client = AsyncMock()
        mock_client.disasm.return_value = [
            {
                "address": 0x08804000,
                "text": "nop",
                "encoding": 0x00000000,
                "branchDelay": False,
                "isBranch": False,
                "name": "nop",
                "params": "",
            }
        ]

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        result = await disassemble(
            session_id="sess-1", address=0x08804000, count=1
        )

        instr = result["instructions"][0]
        assert "text" in instr
        assert "address" in instr
        assert "encoding" not in instr, (
            "D-20: 'encoding' field must be stripped — it's binary "
            "data that bloats the response."
        )
        assert "branchDelay" not in instr
        assert "isBranch" not in instr

    @pytest.mark.asyncio
    async def test_text_reconstructed_from_name_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When text is missing, reconstruct from name+params."""
        mock_client = AsyncMock()
        mock_client.disasm.return_value = [
            {
                "address": 0x08804000,
                "name": "addiu",
                "params": "r5, r0, 0x10",
            }
        ]

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        result = await disassemble(
            session_id="sess-1", address=0x08804000, count=1
        )

        instr = result["instructions"][0]
        assert instr["text"] == "addiu r5, r0, 0x10"


# ============================================================================
# D-08/D-14: funcs/func_scan top_n truncation
# ============================================================================


class TestQueryTopNTruncation:
    """L3: query(funcs/func_scan, top_n=N) truncates results (D-08/D-14).

    hle.func.list can return 762KB+ of function entries. top_n limits
    the number returned.
    """

    @pytest.mark.asyncio
    async def test_funcs_top_n_truncates_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """query(action='funcs', top_n=5) returns at most 5 entries."""
        mock_client = AsyncMock()
        mock_client.func_list.return_value = {
            "functions": [{"name": f"func_{i}"} for i in range(100)]
        }

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.query.session_client",
            fake_session_client,
        )

        result = await query(
            session_id="sess-1", action="funcs", top_n=5
        )

        data = result["data"]
        assert isinstance(data, dict)
        assert len(data["functions"]) == 5, (
            "D-08/D-14: top_n=5 must truncate functions list to 5 entries."
        )

    @pytest.mark.asyncio
    async def test_funcs_top_n_zero_no_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """top_n=0 means no limit (backward compat)."""
        mock_client = AsyncMock()
        mock_client.func_list.return_value = {
            "functions": [{"name": f"func_{i}"} for i in range(50)]
        }

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.query.session_client",
            fake_session_client,
        )

        result = await query(
            session_id="sess-1", action="funcs", top_n=0
        )

        data = result["data"]
        assert len(data["functions"]) == 50, (
            "top_n=0 must not truncate — backward compat."
        )

    @pytest.mark.asyncio
    async def test_func_scan_top_n_truncates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """func_scan with top_n=3 returns 3 entries."""
        mock_client = AsyncMock()
        mock_client.func_scan.return_value = {}
        mock_client.func_list.return_value = {
            "functions": [{"name": f"f_{i}"} for i in range(100)]
        }

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.query.session_client",
            fake_session_client,
        )

        result = await query(
            session_id="sess-1", action="func_scan", address="0x08804000", top_n=3
        )

        data = result["data"]
        assert len(data["functions"]) == 3


# ============================================================================
# D-10/D-30: search_disasm loop search
# ============================================================================


class TestSearchDisasmLoopSearch:
    """L3: search_disasm loops to collect multiple matches (D-10/D-30).

    PPSSPP's memory.searchDisasm returns only the first match per call.
    The tool loops: after finding a match at M, searches from M+4.
    """

    @pytest.mark.asyncio
    async def test_loop_collects_multiple_matches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3 distinct matches → results has 3 entries."""
        mock_client = AsyncMock()
        # Return 3 different addresses, then null.
        addresses = [0x08804000, 0x08804100, 0x08804200]
        call_count = 0

        async def fake_search_disasm(**kwargs: Any) -> dict[str, Any]:
            nonlocal call_count
            if call_count < len(addresses):
                addr = addresses[call_count]
                call_count += 1
                return {"address": addr}
            return {"address": None}

        mock_client.search_disasm.side_effect = fake_search_disasm
        mock_client.disasm.return_value = [{"text": "nop", "name": "nop", "params": ""}]

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.search_disasm.session_client",
            fake_session_client,
        )

        result = await search_disasm(
            session_id="sess-1",
            address=0x08804000,
            match="nop",
            max_results=10,
        )

        assert len(result["results"]) == 3, (
            "D-10/D-30: loop search must collect all 3 matches, not just "
            "the first one."
        )
        assert result["results"][0]["address"] == 0x08804000
        assert result["results"][1]["address"] == 0x08804100
        assert result["results"][2]["address"] == 0x08804200

    @pytest.mark.asyncio
    async def test_loop_detects_wraparound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When PPSSPP re-finds an address, loop terminates."""
        mock_client = AsyncMock()
        # Always return the same address (loop search wraps around).
        mock_client.search_disasm.return_value = {"address": 0x08804000}
        mock_client.disasm.return_value = [{"text": "nop"}]

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.search_disasm.session_client",
            fake_session_client,
        )

        result = await search_disasm(
            session_id="sess-1",
            address=0x08804000,
            match="nop",
            max_results=100,
        )

        # Only 1 result (the second call finds the same address → break).
        assert len(result["results"]) == 1, (
            "D-10/D-30: loop must terminate when PPSSPP wraps around and "
            "re-finds the same address (seen set detection)."
        )

    @pytest.mark.asyncio
    async def test_max_results_caps_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """max_results=2 caps the loop at 2 matches."""
        mock_client = AsyncMock()
        # Always return a new address (never wraps).
        call_count = 0

        async def fake_search_disasm(**kwargs: Any) -> dict[str, Any]:
            nonlocal call_count
            addr = 0x08804000 + call_count * 4
            call_count += 1
            return {"address": addr}

        mock_client.search_disasm.side_effect = fake_search_disasm
        mock_client.disasm.return_value = [{"text": "nop"}]

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.search_disasm.session_client",
            fake_session_client,
        )

        result = await search_disasm(
            session_id="sess-1",
            address=0x08804000,
            match="nop",
            max_results=2,
        )

        assert len(result["results"]) == 2, (
            "D-10/D-30: max_results=2 must cap the loop at 2 matches."
        )

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PPSSPP returns address=null → empty results."""
        mock_client = AsyncMock()
        mock_client.search_disasm.return_value = {"address": None}

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.search_disasm.session_client",
            fake_session_client,
        )

        result = await search_disasm(
            session_id="sess-1",
            address=0x08804000,
            match="nonexistent",
        )

        assert len(result["results"]) == 0
        # search_disasm called once (first call returned null).
        assert mock_client.search_disasm.await_count == 1

    @pytest.mark.asyncio
    async def test_range_search_stops_at_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Range search (end > address) stops when current >= end."""
        mock_client = AsyncMock()
        mock_client.search_disasm.return_value = {"address": None}
        mock_client.disasm.return_value = [{"text": "nop"}]

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.search_disasm.session_client",
            fake_session_client,
        )

        # Range: 0x08804000 to 0x08804100 (256 bytes = 64 instructions).
        await search_disasm(
            session_id="sess-1",
            address=0x08804000,
            match="nop",
            end=0x08804100,
            max_results=100,
        )

        # search_disasm called once (returned null, no match in range).
        assert mock_client.search_disasm.await_count == 1
        # Verify end was forwarded.
        call_kwargs = mock_client.search_disasm.call_args.kwargs
        assert call_kwargs["end"] == 0x08804100
