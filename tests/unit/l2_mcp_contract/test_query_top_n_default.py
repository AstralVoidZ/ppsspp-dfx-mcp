"""L3 orchestration tests: G4 query funcs/func_scan top_n safe default.

Anchor: research_ppsspp_dfx_best_practice_gap_audit_v1 §G4 — top_n
defaulting to 0 (no limit) made a plain ``query(action="funcs")`` able to
return a 700+KB hle.func.list payload. The default is now 100; explicit
``top_n=0`` still means "no limit" (backward compat, covered by
test_output_governance_batch3.TestQueryTopNTruncation).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.tools.query import query

pytestmark = pytest.mark.asyncio


def _mock_func_list(monkeypatch: pytest.MonkeyPatch, count: int) -> AsyncMock:
    mock_client = AsyncMock()
    mock_client.func_list.return_value = {
        "functions": [{"name": f"func_{i}"} for i in range(count)]
    }

    @asynccontextmanager
    async def fake_session_client(
        session_id: str,
    ) -> AsyncIterator[AsyncMock]:
        yield mock_client

    monkeypatch.setattr("ppsspp_dfx_mcp.tools.query.session_client", fake_session_client)
    return mock_client


class TestQueryTopNDefault:
    async def test_funcs_default_truncates_to_100(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Calling funcs WITHOUT top_n truncates to 100 (was unlimited)."""
        _mock_func_list(monkeypatch, 150)
        result = await query(session_id="sess-1", action="funcs")
        data = result["data"]
        assert len(data["functions"]) == 100, (
            "G4: funcs without explicit top_n must default to 100 — "
            "an unbounded hle.func.list can return 700+KB."
        )

    async def test_funcs_below_default_not_padded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A 40-entry function list returns all 40 (no artificial cap)."""
        _mock_func_list(monkeypatch, 40)
        result = await query(session_id="sess-1", action="funcs")
        assert len(result["data"]["functions"]) == 40
