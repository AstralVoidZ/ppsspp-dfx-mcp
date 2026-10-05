"""G-9 (FR-009): ``func_remove`` target-not-found must be a domain error.

Test report finding G-9-Q-ERR-PASSTHROUGH: removing a non-tracked address
surfaced the raw PPSSPP event as ``[PPSSPP_PROTOCOL_ERROR] PPSSPP error:
No function found at 'address' (level=2)`` — a coarse code and a message
that prints the parameter NAME in place of its value, so the agent could
not tell "no such tracked function" from a transport failure.

Fix: the tool maps that one protocol message to ``FuncNotFound``
(``FUNC_NOT_FOUND``) and composes a message carrying the address actually
attempted. Any other protocol error still surfaces unchanged.

Each assertion is falsifiable: deleting the mapping in
``tools/query.py`` turns the first two cases red (the wrong code / the
``'address'`` literal leaks through).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.errors import FuncNotFound, PpssppProtocolError
from ppsspp_dfx_mcp.tools import query as query_mod
from ppsspp_dfx_mcp.tools.query import query

pytestmark = pytest.mark.asyncio

_KNOWN_BAD = "PPSSPP error: No function found at 'address' (level=2)"
_OTHER_PROTOCOL = "PPSSPP error: some other protocol failure (level=2)"


def _patch_client(monkeypatch: pytest.MonkeyPatch, side_effect: Any) -> AsyncMock:
    mock = AsyncMock()
    mock.func_remove.side_effect = side_effect

    @asynccontextmanager
    async def fake_session_client(session_id: str) -> AsyncIterator[AsyncMock]:
        yield mock

    monkeypatch.setattr(query_mod, "session_client", fake_session_client)
    return mock


class TestFuncRemoveDomainError:
    async def test_unknown_target_is_func_not_found_without_param_name_literal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock = _patch_client(monkeypatch, RuntimeError(_KNOWN_BAD))
        with pytest.raises(FuncNotFound) as excinfo:
            await query(session_id="s", action="func_remove", address="0x09FFF000")
        message = str(excinfo.value)
        assert excinfo.value.code == "FUNC_NOT_FOUND"
        # The attempted value, not the parameter name, must be what is shown.
        assert "0x09FFF000" in message
        assert "'address'" not in message and "address" not in message.lower()
        mock.func_remove.assert_awaited_once_with(address=0x09FFF000)

    async def test_other_protocol_error_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_client(monkeypatch, RuntimeError(_OTHER_PROTOCOL))
        with pytest.raises(PpssppProtocolError) as excinfo:
            await query(session_id="s", action="func_remove", address="0x09FFF000")
        assert excinfo.value.code == "PPSSPP_PROTOCOL_ERROR"

    async def test_successful_removal_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock = _patch_client(monkeypatch, None)
        mock.func_remove.return_value = {"action": "func_remove"}
        result = await query(session_id="s", action="func_remove", address="0x09FFF000")
        assert result["data"] == {"action": "func_remove"}
