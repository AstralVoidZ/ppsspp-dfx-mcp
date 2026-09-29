"""W4 (review v3): query(action='func_add') name-only verification.

A name-only call (`address` omitted, explicitly allowed) set `addr=None`,
but the verification compared `f.get("address") == (addr or 0)` — i.e. it
checked "does an address==0 entry exist": usually a false negative, and a
false POSITIVE when the symbol table happens to hold an address-0 entry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.tools import query as query_mod
from ppsspp_dfx_mcp.tools.query import query


def _patch_client(monkeypatch: pytest.MonkeyPatch, functions: list[dict]) -> AsyncMock:
    mock = AsyncMock()
    mock.func_add.return_value = {"action": "func_add"}
    mock.func_list.return_value = {"functions": functions}

    @asynccontextmanager
    async def fake_session_client(session_id: str) -> AsyncIterator[AsyncMock]:
        yield mock

    monkeypatch.setattr(query_mod, "session_client", fake_session_client)
    return mock


async def test_name_only_verified_true_when_name_present(monkeypatch: pytest.MonkeyPatch):
    _patch_client(monkeypatch, [{"name": "user_main", "address": 0x08804000}])
    result = await query(session_id="s", action="func_add", name="user_main")
    # Pre-fix: verified is False (the check looked for address == 0).
    assert result["data"]["verified"] is True


async def test_name_only_not_verified_by_address_zero_entry(monkeypatch: pytest.MonkeyPatch):
    _patch_client(monkeypatch, [{"name": "other", "address": 0}])
    result = await query(session_id="s", action="func_add", name="user_main")
    # Pre-fix: verified is True — a false positive from the address==0 row.
    assert result["data"]["verified"] is False
    assert "verified_note" in result["data"]


async def test_address_based_verification_unchanged(monkeypatch: pytest.MonkeyPatch):
    _patch_client(monkeypatch, [{"name": "user_main", "address": 0x08804000}])
    result = await query(session_id="s", action="func_add", name="user_main", address="0x08804000")
    assert result["data"]["verified"] is True
