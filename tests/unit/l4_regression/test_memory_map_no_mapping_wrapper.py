"""L4 regression tests for V016.

Violation:
- V016 [MEDIUM]: `memory_map` wrapped the PPSSPP response in an extra
  `mapping` key (`return {"mapping": mapping_resp}`). PPSSPP
  `memory.mapping` returns `ranges`: array of `{type, subtype, name,
  address, size}` — there is no `mapping` wrapper at the protocol level.
  See MemoryInfoSubscriber.cpp:L48, L78-129. The wrapper forced the tool
  layer to unwrap `response["mapping"]` and broke the principle that
  DebugClient forwards PPSSPP responses verbatim.

Fix: 4-layer coordinated change:
1. DebugClient.memory_map() returns the raw transport response.
2. tools/memory_map.py uses the response directly (no `mapping` unwrap).
3. models/memory_map.py — MemoryMapResult.mapping field unchanged.
4. views/memory_map.py — _extract_ranges/from_result unchanged.

The final tool output structure (`{ranges, mapping, text}`) is
unchanged — only the DebugClient return shape and the tool's unwrap
logic changed.

Anchor:
- L1: WS event `memory.mapping` returns a `ranges` array.
- L4: DebugClient.memory_map source contains NO `{"mapping":` wrapper.
- L4: tool layer extracts `ranges` from the raw response correctly.
"""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient
from ppsspp_dfx_mcp.tools.memory_map import memory_map


class TestV016MemoryMapNoMappingWrapper:
    """V016: DebugClient.memory_map returns the raw PPSSPP response."""

    @pytest.mark.asyncio
    async def test_memory_map_forwards_to_memory_mapping_event(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """L1 anchor: memory_map() forwards to `memory.mapping`."""
        transport.set_response("memory.mapping", {"ranges": []})

        await client.memory_map()

        assert transport.calls[-1][0] == "memory.mapping"
        assert transport.calls[-1][1] == {}

    @pytest.mark.asyncio
    async def test_memory_map_returns_raw_response_dict(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """L4 anchor: returns the raw PPSSPP response dict.

        The V016 fix removed the `{"mapping": ...}` wrapper. The result
        is the raw PPSSPP response containing the `ranges` key — NOT a
        dict with a `mapping` key wrapping it.
        """
        ranges = [
            {"type": "ram", "subtype": "primary", "name": "User Memory",
             "address": 0x08800000, "size": 0x01800000},
            {"type": "vram", "subtype": "primary", "name": "VRAM",
             "address": 0x04000000, "size": 0x00200000},
        ]
        transport.set_response("memory.mapping", {"ranges": ranges})

        result = await client.memory_map()

        assert result == {"ranges": ranges}
        # V016 regression guard: must NOT be wrapped in `{"mapping": ...}`.
        assert "mapping" not in result, (
            "memory_map must NOT wrap the response in a `mapping` key — "
            "V016 fix was reverted. The raw PPSSPP response should be "
            "returned directly."
        )

    def test_memory_map_source_has_no_mapping_wrapper(self) -> None:
        """L4 anchor: `memory_map` source must NOT wrap in `{"mapping":`.

        Inspects the live source of `memory_map`. If the V016 fix is
        reverted (re-introducing `return {"mapping": ...}`), this
        assertion fails immediately.
        """
        src = inspect.getsource(PpssppDebugClient.memory_map)
        assert '{"mapping":' not in src, (
            "memory_map source must NOT contain a `{'mapping': ...}` "
            "wrapper — V016 fix was reverted. The raw transport "
            "response should be returned directly. See "
            "MemoryInfoSubscriber.cpp:L48, L78-129."
        )
        assert 'mapping_resp' not in src, (
            "memory_map source must NOT use a `mapping_resp` local "
            "variable — V016 fix was reverted (wraps the response in a "
            "`mapping` key)."
        )

    @pytest.mark.asyncio
    async def test_tool_layer_extracts_ranges_from_raw_response(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """L4 anchor: tool layer extracts `ranges` from the raw response.

        Integration test: the tool layer (`tools/memory_map.py`) must
        work with the new DebugClient return shape (raw response with
        `ranges` key, no `mapping` wrapper). The final tool output
        structure (`{ranges, mapping, text}`) must remain unchanged.
        """
        ranges = [
            {"type": "ram", "subtype": "primary", "name": "User Memory",
             "address": 0x08800000, "size": 0x01800000},
        ]
        mock_client = AsyncMock()
        # V016 fix: client returns the raw PPSSPP response (no wrapper).
        mock_client.memory_map.return_value = {"ranges": ranges}

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory_map.session_client",
            fake_session_client,
        )

        result = await memory_map(session_id="sess-1")

        # Final tool output structure unchanged: {ranges, mapping, text}.
        assert result["ranges"] == ranges
        # `mapping` field retains the raw response (now without wrapper).
        assert result["mapping"] == {"ranges": ranges}
        assert "0x08800000" in result["text"]
