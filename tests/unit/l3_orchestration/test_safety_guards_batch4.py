"""L3 orchestration tests: safety guards (batch 4).

Anchors:
- D-19: disassemble count<=0 returns empty without calling PPSSPP
- D-05: write_memory protected address check + force override
- D-21: mem_update merges read/write/change from current state
- D-31: session start port conflict detection

L3 focus (tool wrapper orchestration, NOT WS forwarding):
- disassemble: count=0 returns empty instructions, client.disasm NOT called
- write_memory: address in kernel/code range raises ToolError; force=True bypasses
- breakpoint(mem_update): partial read/write triggers mem_bp_list query + merge
- session_manager: _check_port_conflict raises PortConflict on duplicate port
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.tools.memory import disassemble

# ============================================================================
# D-19: disassemble count<=0 returns empty
# ============================================================================


class TestDisassembleCountZero:
    """L3: disassemble(count=0) returns empty result without calling PPSSPP."""

    @pytest.mark.asyncio
    async def test_count_zero_defaults_to_ten(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """M2 (2026-09-19): count=0 falls back to the documented default 10.

        The original D-19 lock sent count<=0 straight through as an empty
        result; a live session read that as "unmapped memory". The guard's
        real purpose — never send count=0 to PPSSPP ("Missing end
        parameter") — is preserved by remapping to the default instead.
        """
        mock_client = AsyncMock()
        mock_client.disasm.return_value = [
            {"address": 0x08804000 + i * 4, "text": f"nop{i}"} for i in range(10)
        ]

        @asynccontextmanager
        async def fake_session_client(session_id: str) -> AsyncIterator[AsyncMock]:
            yield mock_client

        async def fake_resolve(session_id):
            return session_id or "sess-1"

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.resolve_session_id", fake_resolve
        )
        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client", fake_session_client
        )
        result = await disassemble(session_id="sess-1", address=0x08804000, count=0)

        assert result["count"] == 10
        assert len(result["instructions"]) == 10
        assert mock_client.disasm.await_count == 1
        # The PPSSPP-facing count must be the remapped default, never 0.
        assert mock_client.disasm.await_args.kwargs.get("count", 10) == 10

    @pytest.mark.asyncio
    async def test_count_negative_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """count=-1 → empty result, client.disasm NOT called."""
        mock_client = AsyncMock()

        async def fake_session_client(session_id: str) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client", fake_session_client
        )
