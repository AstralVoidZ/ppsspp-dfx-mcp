"""L1 contract tests for system methods.

Anchors (PPSSPP C++ source):
- game.status: GameSubscriber.cpp (no params; transports timeout to WS layer)
- memory.mapping: returns `ranges` array of memory regions
  (ram / vram / sram, primary / mirror) — no params
- memory.base: DisasmSubscriber.cpp:L57, L263-267
  (no params; returns `addressHex` 16-digit hex string)
- game.reset: GameSubscriber.cpp:L26, L41-62
  (optional `break` bool; Python param named `break_` because
  `break` is a keyword)

L1 tests assert pure forwarding behavior: each DebugClient method
forwards the correct PPSSPP WebSocket event name + parameters, and
extracts the correct return field where applicable. Uses the
canonical FakeTransport (configured via the `transport` and `client`
fixtures in conftest.py).
"""

from __future__ import annotations

import pytest


class TestSystemContract:
    """L1 contract: system methods forward correct event + params."""

    @pytest.mark.asyncio
    async def test_game_status_forwards_event(
        self, client, transport
    ):
        """L1 anchor: GameSubscriber.cpp (game.status).

        game_status() forwards to the `game.status` WS event with no
        game-side params. The transport-level timeout (5.0s) is
        consumed by FakeTransport.call's named `timeout` parameter and
        does NOT appear in the recorded `**params`.
        """
        transport.set_response("game.status", {"running": True, "title": "TOPX"})
        await client.game_status()
        assert transport.calls[-1][0] == "game.status"
        assert transport.calls[-1][1] == {}

    @pytest.mark.asyncio
    async def test_memory_map_forwards_event_and_passes_response(
        self, client, transport
    ):
        """L1 anchor: memory.mapping (no params; returns `ranges`).

        memory_map() forwards to the `memory.mapping` WS event with
        no params. The response (containing the `ranges` array) is
        passed through verbatim — the method does not unwrap or
        re-shape the response.
        """
        ranges_payload = [
            {"name": "ram", "address": "0x08800000", "size": "0x02000000"},
            {"name": "vram", "address": "0x04000000", "size": "0x00200000"},
        ]
        transport.set_response("memory.mapping", {"ranges": ranges_payload})
        result = await client.memory_map()
        assert transport.calls[-1][0] == "memory.mapping"
        assert transport.calls[-1][1] == {}
        assert result == {"ranges": ranges_payload}


    @pytest.mark.asyncio
    async def test_reset_no_break_forwards_empty_params(
        self, client, transport
    ):
        """L1 anchor: GameSubscriber.cpp:L26, L41-62 (game.reset).

        reset() with no args forwards to `game.reset` with no params.
        The optional `break` field is omitted entirely when `break_`
        is None (not forwarded as null).
        """
        transport.set_response("game.reset", {"ok": True})
        await client.reset()
        assert transport.calls[-1][0] == "game.reset"
        assert transport.calls[-1][1] == {}

    @pytest.mark.asyncio
    async def test_reset_with_break_forwards_param(
        self, client, transport
    ):
        """L1 anchor: GameSubscriber.cpp:L26, L41-62 (game.reset, break=True).

        reset(break_=True) forwards `break=True` to the `game.reset`
        WS event. The Python param is named `break_` (trailing
        underscore) because `break` is a keyword; it is forwarded
        under the WS name `break`.
        """
        transport.set_response("game.reset", {"ok": True})
        await client.reset(break_=True)
        assert transport.calls[-1][0] == "game.reset"
        assert transport.calls[-1][1] == {"break": True}
