"""write_memory accepts the family-conventional `value` (D5).

US7 re-verification found D5 to be the ONLY genuine finding among the
self-description items: write_memory named its payload `data` while its
siblings use `value`/`size`, so a caller reaching for the family
convention got "data: Missing key" on the first attempt.

`value` is an ALIAS; `data` stays canonical and wins when both are given,
so no existing caller breaks and the published schema only grows.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest


def _stub(client):
    @asynccontextmanager
    async def _cm(_sid):
        yield client

    return patch("ppsspp_dfx_mcp.tools.memory.session_client", _cm)


def _write_calls(transport):
    return [(n, p) for n, p in transport.calls if n.startswith("memory.write")]


def _written_int(transport):
    """The integer the tool actually sent over the wire."""
    for name, params in reversed(transport.calls):
        if name.startswith("memory.write") and "value" in params:
            return params["value"]
    return None


class TestValueAlias:
    def test_both_parameters_are_published(self) -> None:
        """The alias must be visible in the schema, not merely accepted."""
        from ppsspp_dfx_mcp import server as srv

        srv.register_all_tools()
        tools = {t.name: t for t in asyncio.run(srv.mcp.list_tools())}
        props = (tools["ppsspp_write_memory"].input_schema or {}).get("properties", {})
        assert "data" in props, "canonical `data` disappeared"
        assert "value" in props, "alias `value` is not published"

    @pytest.mark.asyncio
    async def test_value_alias_writes_same_bytes(self, client, transport) -> None:
        """`value=` must actually reach the write, not just validate."""
        from ppsspp_dfx_mcp.tools.memory import write_memory

        transport.set_response("memory.write_u32", {})
        with _stub(client):
            out = await write_memory(
                session_id="s",
                address="0x09000000",
                value="0xAABBCCDD",
                format="u32",
            )
        assert out["bytes_written"] == 4
        assert _write_calls(transport), "no memory.write* call was issued"
        assert _written_int(transport) == 0xAABBCCDD, (
            f"alias payload not forwarded, wire value={_written_int(transport)!r}"
        )

    @pytest.mark.asyncio
    async def test_data_wins_when_both_given(self, client, transport) -> None:
        """`data` is canonical: it must not be silently overridden."""
        from ppsspp_dfx_mcp.tools.memory import write_memory

        transport.set_response("memory.write_u32", {})
        with _stub(client):
            await write_memory(
                session_id="s",
                address="0x09000000",
                data="0x11111111",
                value="0x22222222",
                format="u32",
            )
        assert _written_int(transport) == 0x11111111, (
            f"canonical `data` must win, wire value={_written_int(transport)!r}"
        )

    @pytest.mark.asyncio
    async def test_missing_payload_still_reports_data(self, client, transport) -> None:
        """Omitting both must still fail, naming the canonical parameter."""
        from ppsspp_dfx_mcp.tools.memory import write_memory

        with pytest.raises(Exception) as exc:
            await write_memory(session_id="s", address="0x09000000", format="u32")
        msg = str(exc.value)
        assert "data" in msg, f"error must name the missing payload, got: {msg}"
