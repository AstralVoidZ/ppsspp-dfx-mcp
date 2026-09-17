"""L4 regression tests for V011.

Violation:
- V011 [MEDIUM]: `press_button` had `duration` default=10 in both
  DebugClient and tool layer, but the PPSSPP `input.buttons.press`
  contract specifies default=1. See InputSubscriber.cpp:L93, L170-192.

Fix: both DebugClient.press_button and tools.input.press_button
changed `duration` default from 10 to 1.

Anchor:
- L4: DebugClient.press_button duration default == 1.
- L4: tools.input.press_button duration default == 1.
- L1: default call forwards duration=1 to input.buttons.press.
"""

from __future__ import annotations

import inspect

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient
from ppsspp_dfx_mcp.tools.input import press_button as tool_press_button


class TestV011PressButtonDurationDefault:
    """V011: press_button duration default must be 1 (not 10)."""

    def test_debug_client_duration_default_is_1(self):
        """L4 anchor: DebugClient.press_button duration default == 1."""
        sig = inspect.signature(PpssppDebugClient.press_button)
        params = sig.parameters
        assert "duration" in params, "press_button signature missing `duration` param."
        assert params["duration"].default == 1, (
            f"DebugClient.press_button `duration` default should be 1, "
            f"got {params['duration'].default!r}. See "
            "InputSubscriber.cpp:L93, L170-192."
        )

    def test_tool_duration_default_is_1(self):
        """L4 anchor: tools.input.press_button duration default == 1."""
        sig = inspect.signature(tool_press_button)
        params = sig.parameters
        assert "duration" in params, "tool press_button signature missing `duration` param."
        assert params["duration"].default == 1, (
            f"tool press_button `duration` default should be 1, got {params['duration'].default!r}."
        )

    @pytest.mark.asyncio
    async def test_default_call_forwards_duration_1(self, client, transport):
        """L1 anchor: press_button('cross') forwards duration=1."""
        transport.set_response("input.buttons.press", {"ok": True})
        await client.press_button("cross")
        assert transport.calls[-1][0] == "input.buttons.press"
        assert transport.calls[-1][1] == {
            "button": "cross",
            "duration": 1,
        }

    @pytest.mark.asyncio
    async def test_explicit_duration_5_forwards(self, client, transport):
        """L1 anchor: press_button('cross', duration=5) forwards duration=5."""
        transport.set_response("input.buttons.press", {"ok": True})
        await client.press_button("cross", duration=5)
        assert transport.calls[-1][0] == "input.buttons.press"
        assert transport.calls[-1][1] == {
            "button": "cross",
            "duration": 5,
        }
