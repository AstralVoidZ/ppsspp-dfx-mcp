"""L3 orchestration tests: search_disasm protocol-adapter layer (batch 2).

Anchors D-24 + D-23 (batch 2 protocol-adapter fixes):
- D-24: tool wrapper strips '$' from match before forwarding to PPSSPP
  (PPSSPP register names have no '$' prefix).
- D-23: tool wrapper calls client.disasm() on the matched address to
  populate the 'text' field (PPSSPP memory.searchDisasm only returns
  {"address": <u32>|null}, no instruction text).

L3 focus (NOT covered by L1 — L1 tests the client layer's WS forwarding;
L3 tests the tool wrapper's protocol adaptation):
- '$' stripping: "jr $ra" → forwarded as "jr ra"
- text population: matched_addr → client.disasm(addr, count=1) → entry["text"]
- disasm failure does not mask the match (entry still has address)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.tools.search_disasm import search_disasm


class TestSearchDisasmDollarStrip:
    """L3: tool wrapper strips '$' from match before forwarding (D-24).

    PPSSPP's MIPSDebugInterface omits '$' from register names
    (MIPSDebugInterface.cpp:281-290). Callers may use idiomatic MIPS
    syntax like "jr $ra"; the tool strips '$' so it matches PPSSPP's
    "jr ra" output.
    """

    @pytest.mark.asyncio
    async def test_dollar_in_register_name_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """match="jr $ra" forwarded as match="jr ra" to client.search_disasm."""
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

        await search_disasm(
            session_id="sess-1", address=0x08804000, match="jr $ra"
        )

        # Verify '$' was stripped before forwarding.
        call_kwargs = mock_client.search_disasm.call_args.kwargs
        assert call_kwargs["match"] == "jr ra", (
            "tool wrapper must strip '$' from match — 'jr $ra' should be "
            "forwarded as 'jr ra' (PPSSPP register names have no '$')."
        )

    @pytest.mark.asyncio
    async def test_no_dollar_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """match without '$' is forwarded unchanged."""
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

        await search_disasm(
            session_id="sess-1", address=0x08804000, match="jal func"
        )

        call_kwargs = mock_client.search_disasm.call_args.kwargs
        assert call_kwargs["match"] == "jal func"

    @pytest.mark.asyncio
    async def test_multiple_dollars_all_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """match="addu $t0, $t1, $t2" → forwarded as "addu t0, t1, t2"."""
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

        await search_disasm(
            session_id="sess-1",
            address=0x08804000,
            match="addu $t0, $t1, $t2",
        )

        call_kwargs = mock_client.search_disasm.call_args.kwargs
        assert call_kwargs["match"] == "addu t0, t1, t2"


class TestSearchDisasmTextPopulation:
    """L3: tool wrapper populates 'text' via client.disasm() (D-23).

    PPSSPP memory.searchDisasm returns only {"address": <u32>|null} —
    no instruction text. The tool calls client.disasm(matched_addr,
    count=1) to get the instruction text and injects it into the
    result entry.
    """

    @pytest.mark.asyncio
    async def test_matched_address_populates_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When searchDisasm returns an address, disasm is called for text."""
        mock_client = AsyncMock()
        mock_client.search_disasm.return_value = {"address": 0x08808400}
        mock_client.disasm.return_value = [
            {"text": "jr ra", "name": "jr", "params": "ra"}
        ]

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
            session_id="sess-1", address=0x08804000, match="jr ra"
        )

        # Verify disasm was called with the matched address.
        mock_client.disasm.assert_awaited_once_with(
            address=0x08808400, count=1
        )
        # Verify the result entry has the text field populated.
        assert len(result["results"]) == 1
        assert result["results"][0]["address"] == 0x08808400
        assert result["results"][0]["text"] == "jr ra", (
            "tool wrapper must populate 'text' field via client.disasm() — "
            "PPSSPP memory.searchDisasm returns only address, no text."
        )

    @pytest.mark.asyncio
    async def test_no_match_no_disasm_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When searchDisasm returns address=null, disasm is NOT called."""
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
            session_id="sess-1", address=0x08804000, match="nonexistent"
        )

        mock_client.disasm.assert_not_awaited()
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_disasm_failure_does_not_mask_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If disasm() raises, the match address is still returned.

        The tool must not mask a successful searchDisasm match with a
        disasm failure — the address is valuable even without text.
        """
        mock_client = AsyncMock()
        mock_client.search_disasm.return_value = {"address": 0x08808400}
        mock_client.disasm.side_effect = RuntimeError("disasm failed")

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
            session_id="sess-1", address=0x08804000, match="jr ra"
        )

        # Match is still returned, just without text.
        assert len(result["results"]) == 1
        assert result["results"][0]["address"] == 0x08808400
        assert "text" not in result["results"][0] or not result["results"][0].get("text")
