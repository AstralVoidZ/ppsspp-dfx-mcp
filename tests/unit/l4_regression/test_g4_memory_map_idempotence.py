"""G-4 (FR-004): memory_map business fields must be stable across calls.

Deep-test case P6-05 (mcp_test_report/tools/ppsspp_memory_map.md): ten
identical ppsspp_memory_map calls returned byte-identical memory layouts,
but structuredContent.mapping.ticket advanced t30→t39 — whole-payload
idempotence comparison could never succeed.

Fix: the volatile per-call `ticket` is stripped from `mapping` in the
view; the remaining business fields (ranges / mapping / text) are
byte-stable, and the client's raw response dict is left untouched.

Two-way acceptance (spec.md FR-004): two consecutive calls MUST compare
byte-identical on business fields; the real business content (ranges,
event) MUST survive the strip.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from ppsspp_dfx_mcp.tools import memory_map as memory_map_mod
from ppsspp_dfx_mcp.tools.memory_map import memory_map

RANGES = [
    {
        "type": "ram",
        "subtype": "primary",
        "name": "User Memory",
        "address": 0x08800000,
        "size": 0x01800000,
    },
]


class _FakeClient:
    """Returns a fresh PPSSPP-shaped reply with an advancing ticket."""

    def __init__(self) -> None:
        self.tickets = ["t30", "t31"]
        self.raw_responses: list[dict[str, Any]] = []

    async def memory_map(self) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "event": "memory.mapping",
            "ticket": self.tickets.pop(0),
            "ranges": RANGES,
        }
        self.raw_responses.append(raw)
        return raw


def _patch_client(monkeypatch) -> _FakeClient:
    client = _FakeClient()

    @asynccontextmanager
    async def _fake_session_client(_session_id):
        yield client

    monkeypatch.setattr(memory_map_mod, "session_client", _fake_session_client)
    return client


class TestTicketStripped:
    async def test_ticket_is_absent_from_business_fields(self, monkeypatch) -> None:
        _patch_client(monkeypatch)
        out = await memory_map(session_id="sess-1")
        assert "ticket" not in out["mapping"], (
            f"volatile ticket leaks into business fields: {out['mapping']!r}"
        )

    async def test_two_consecutive_calls_are_byte_identical(self, monkeypatch) -> None:
        """P6-05 acceptance: whole payload (all business fields) equal."""
        _patch_client(monkeypatch)
        first = await memory_map(session_id="sess-1")
        second = await memory_map(session_id="sess-1")
        assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)

    async def test_business_content_survives(self, monkeypatch) -> None:
        client = _patch_client(monkeypatch)
        out = await memory_map(session_id="sess-1")
        assert out["mapping"]["event"] == "memory.mapping"
        assert out["ranges"][0]["address"] == "0x08800000"
        assert out["ranges"][0]["name"] == "User Memory"
        assert "0x08800000-0x0A000000" in out["text"]
        # The strip must not mutate the client's raw response dict.
        assert client.raw_responses[0]["ticket"] == "t30"
