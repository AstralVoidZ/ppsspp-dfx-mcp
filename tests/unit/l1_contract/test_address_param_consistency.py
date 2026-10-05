"""Address-parameter naming consistency (US1 / contract C1.3).

D14 (measured 2026-09-30): `ppsspp_diff_memory` required `start` + `end`
while `read_memory`, `write_memory` and `scan` all name the start `address`.
A caller who had just used the sibling tools got an unhelpful
"action='snapshot' requires start and end" with no example.

The contract is not "rename everything" — it is "a caller who reached for
the family convention is not punished, and a missing argument explains
both accepted forms".
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest

from ppsspp_dfx_mcp.errors import ArgsInvalid


def _stub_session(client):
    """Point tools.diff.session_client at an already-built fake client."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm(_session_id):
        yield client

    return patch("ppsspp_dfx_mcp.tools.diff.session_client", _cm)


class TestDiffRangeParameterForms:
    @pytest.mark.asyncio
    async def test_address_size_form_is_accepted(self, client, transport) -> None:
        """The family convention (address + size) must work."""
        from ppsspp_dfx_mcp.tools.diff import diff_memory

        transport.set_response("memory.read", {"value": 0, "size": 4})
        with _stub_session(client):
            out = await diff_memory(
                action="snapshot", session_id="s", address="0x09000000", size=64
            )
        assert out["size_bytes"] == 64
        assert out["start"] == "0x09000000"

    @pytest.mark.asyncio
    async def test_start_end_form_still_works(self, client, transport) -> None:
        """Renaming must not break the documented form."""
        from ppsspp_dfx_mcp.tools.diff import diff_memory

        transport.set_response("memory.read", {"value": 0, "size": 4})
        with _stub_session(client):
            out = await diff_memory(
                action="snapshot", session_id="s", start="0x09000000", end="0x09000040"
            )
        assert out["size_bytes"] == 64
        with contextlib.suppress(Exception):
            await diff_memory(action="drop", session_id="s", handle=out["handle"])

    @pytest.mark.asyncio
    async def test_both_forms_agree(self, client, transport) -> None:
        """address+size and start+end must produce the same range."""
        from ppsspp_dfx_mcp.tools.diff import diff_memory

        transport.set_response("memory.read", {"value": 0, "size": 4})
        with _stub_session(client):
            a = await diff_memory(action="snapshot", session_id="s", address="0x09000000", size=64)
            b = await diff_memory(
                action="snapshot", session_id="s", start="0x09000000", end="0x09000040"
            )
        assert a["size_bytes"] == b["size_bytes"] == 64
        assert a["start"] == b["start"]
        for out in (a, b):
            with contextlib.suppress(Exception):
                await diff_memory(action="drop", session_id="s", handle=out["handle"])

    @pytest.mark.asyncio
    async def test_missing_range_names_both_forms(self, client, transport) -> None:
        """The error must teach the caller, not just report absence."""
        from ppsspp_dfx_mcp.tools.diff import diff_memory

        transport.set_response("memory.read", {"value": 0, "size": 4})
        with _stub_session(client), pytest.raises(ArgsInvalid) as exc:
            await diff_memory(action="snapshot", session_id="s")
        msg = str(exc.value)
        assert "start" in msg and "end" in msg, msg
        assert "address" in msg and "size" in msg, msg
        assert "0x" in msg, f"error must carry a usable example, got: {msg}"

    @pytest.mark.asyncio
    async def test_half_a_range_is_rejected_with_guidance(self, client, transport) -> None:
        """start without end (or vice versa) must not silently mean 'to the end'."""
        from ppsspp_dfx_mcp.tools.diff import diff_memory

        transport.set_response("memory.read", {"value": 0, "size": 4})
        with _stub_session(client), pytest.raises(ArgsInvalid) as exc:
            await diff_memory(action="snapshot", session_id="s", start="0x09000000")
        assert "0x" in str(exc.value)

    @pytest.mark.asyncio
    async def test_zero_size_is_rejected(self, client, transport) -> None:
        """size=0 would ask for a zero-length snapshot; that is a mistake."""
        from ppsspp_dfx_mcp.tools.diff import diff_memory

        transport.set_response("memory.read", {"value": 0, "size": 4})
        with _stub_session(client), pytest.raises(ArgsInvalid):
            await diff_memory(action="snapshot", session_id="s", address="0x09000000", size=0)
