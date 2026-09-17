"""L3 orchestration tests: G3 optional session_id auto-resolution.

Anchor: research_ppsspp_dfx_best_practice_gap_audit_v1 §G3 — every tool
required an explicit session_id, though the overwhelmingly common case is
exactly one active session. High-frequency tools now auto-resolve:
explicit id passes through; 0 sessions raise a start hint; 2+ sessions
raise SESSION_AMBIGUOUS listing every id. Destructive / low-frequency
tools keep the required contract.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.errors import SessionAmbiguous, ToolError
from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session import session_manager
from ppsspp_dfx_mcp.session.client_helper import resolve_session_id
from ppsspp_dfx_mcp.tools.memory import read_memory

pytestmark = pytest.mark.asyncio


def _session(sid: str) -> Session:
    return Session(session_id=sid, iso_path="game.iso")


def _patch_list(monkeypatch: pytest.MonkeyPatch, sids: list[str]) -> AsyncMock:
    mock = AsyncMock(return_value=[_session(s) for s in sids])
    monkeypatch.setattr(session_manager, "list_sessions", mock)
    return mock


class TestResolveSessionId:
    async def test_explicit_id_passes_through_without_lookup(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_list = _patch_list(monkeypatch, [])
        assert await resolve_session_id("sess-explicit") == "sess-explicit"
        mock_list.assert_not_awaited()

    async def test_no_sessions_raises_start_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_list(monkeypatch, [])
        with pytest.raises(ToolError) as exc:
            await resolve_session_id(None)
        assert exc.value.code == "ARGS_INVALID"
        assert "no active session" in str(exc.value)
        assert 'ppsspp_session(action="start"' in str(exc.value)

    async def test_single_session_resolves_silently(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_list(monkeypatch, ["only-one"])
        assert await resolve_session_id(None) == "only-one"

    async def test_multiple_sessions_raise_ambiguous(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_list(monkeypatch, ["sess-a", "sess-b"])
        with pytest.raises(SessionAmbiguous) as exc:
            await resolve_session_id(None)
        assert "sess-a" in str(exc.value)
        assert "sess-b" in str(exc.value)
        assert "pass session_id explicitly" in str(exc.value)


class TestReadMemoryAutoResolve:
    async def _run(self, monkeypatch: pytest.MonkeyPatch, sids: list[str]):
        _patch_list(monkeypatch, sids)
        mock_client = AsyncMock()
        mock_client.read_bytes.return_value = b"AB"

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr("ppsspp_dfx_mcp.tools.memory.session_client", fake_session_client)
        return await read_memory(action="read_bytes", address="0x08804000", size=2)

    async def test_omitted_id_resolves_and_reads(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = await self._run(monkeypatch, ["auto-sess"])
        assert result["value"] == [65, 66]

    async def test_omitted_id_with_two_sessions_fails_ambiguous(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(SessionAmbiguous):
            await self._run(monkeypatch, ["sess-a", "sess-b"])
