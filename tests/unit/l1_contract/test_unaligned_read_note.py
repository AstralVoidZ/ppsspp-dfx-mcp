"""Unaligned multi-byte reads must be announced (US1 / contract C1.5).

D4 (measured 2026-09-30): `read_u32` at `0x08804001` returned a value with
no remark. On PSP an unaligned 4-byte read succeeds, which is exactly why
it is dangerous — it hides a pointer/offset bug behind a plausible number.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest


def _stub(client):
    @asynccontextmanager
    async def _cm(_sid):
        yield client

    return patch("ppsspp_dfx_mcp.tools.memory.session_client", _cm)


class TestUnalignedReadAnnotation:
    @pytest.mark.asyncio
    async def test_unaligned_u32_read_is_flagged(self, client, transport) -> None:
        from ppsspp_dfx_mcp.tools.memory import read_memory

        transport.set_response("memory.read_u32", {"value": 0x64680000})
        with _stub(client):
            out = await read_memory(action="read_u32", address="0x08804001", session_id="s")
        assert "unaligned" in out["text"].lower(), (
            f"unaligned read must be announced, text={out['text']!r}"
        )

    @pytest.mark.asyncio
    async def test_aligned_u32_read_is_not_flagged(self, client, transport) -> None:
        """The warning must not become noise on every ordinary read."""
        from ppsspp_dfx_mcp.tools.memory import read_memory

        transport.set_response("memory.read_u32", {"value": 0x68000044})
        with _stub(client):
            out = await read_memory(action="read_u32", address="0x08804000", session_id="s")
        assert "unaligned" not in out["text"].lower(), (
            f"aligned read must stay unannotated, text={out['text']!r}"
        )

    @pytest.mark.asyncio
    async def test_annotation_does_not_hide_the_value(self, client, transport) -> None:
        """Appending the warning must not cost the caller the payload."""
        from ppsspp_dfx_mcp.tools.memory import read_memory

        transport.set_response("memory.read_u32", {"value": 0x64680000})
        with _stub(client):
            out = await read_memory(action="read_u32", address="0x08804001", session_id="s")
        assert out["value"] == 0x64680000
        assert out["address"] == "0x08804001"
