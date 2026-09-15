"""L1 contract tests for input methods.

Anchors (PPSSPP C++ source):
- input.buttons.press: InputSubscriber.cpp:L93, L170-192
  (button name + duration in frames; default duration=1)
- input.buttons.send: InputSubscriber.cpp:L94
  (buttons dict: name -> held state; STATE-CHANGE event)
- input.analog.send: InputSubscriber.cpp:L94, L242-256
  (x + y in [-1.0, 1.0] + stick name; default stick="left")

L1 tests assert pure forwarding behavior: each DebugClient method
forwards the correct PPSSPP WebSocket event name + parameters. Uses
the canonical FakeTransport (configured via the `transport` and
`client` fixtures in conftest.py).
"""

from __future__ import annotations

import pytest


class TestInputContract:
    """L1 contract: input methods forward correct event + params."""

    @pytest.mark.asyncio
    async def test_press_button_forwards_event_and_params(
        self, client, transport
    ):
        """L1 anchor: InputSubscriber.cpp:L93 (input.buttons.press).

        press_button("cross", duration=5) forwards the button name and
        the explicit duration value verbatim.
        """
        transport.set_response("input.buttons.press", {"ok": True})
        await client.press_button("cross", duration=5)
        assert transport.calls[-1][0] == "input.buttons.press"
        assert transport.calls[-1][1] == {
            "button": "cross",
            "duration": 5,
        }

    @pytest.mark.asyncio
    async def test_press_button_default_duration_is_1(
        self, client, transport
    ):
        """L1 anchor: InputSubscriber.cpp:L93, L170-192 (default duration=1).

        press_button("circle") with no explicit duration forwards
        duration=1 — matches the PPSSPP `input.buttons.press` contract
        default (one frame).
        """
        transport.set_response("input.buttons.press", {"ok": True})
        await client.press_button("circle")
        assert transport.calls[-1][0] == "input.buttons.press"
        assert transport.calls[-1][1] == {
            "button": "circle",
            "duration": 1,
        }

    @pytest.mark.asyncio
    async def test_hold_buttons_forwards_dict_param(
        self, client, transport
    ):
        """L1 anchor: InputSubscriber.cpp:L94 (input.buttons.send).

        hold_buttons({"cross": True, "circle": True}) forwards the
        buttons dict under the `buttons` key. STATE-CHANGE event.
        """
        transport.set_response("input.buttons.send", {"ok": True})
        buttons = {"cross": True, "circle": True}
        await client.hold_buttons(buttons)
        assert transport.calls[-1][0] == "input.buttons.send"
        assert transport.calls[-1][1] == {"buttons": buttons}

    @pytest.mark.asyncio
    async def test_send_analog_default_stick_is_left(
        self, client, transport
    ):
        """L1 anchor: InputSubscriber.cpp:L94, L242-256 (default stick="left").

        send_analog(0.5, -0.25) with no explicit stick forwards
        stick="left" — matches the PPSSPP `input.analog.send` contract
        default.
        """
        transport.set_response("input.analog.send", {"ok": True})
        await client.send_analog(0.5, -0.25)
        assert transport.calls[-1][0] == "input.analog.send"
        assert transport.calls[-1][1] == {
            "x": 0.5,
            "y": -0.25,
            "stick": "left",
        }

    @pytest.mark.asyncio
    async def test_send_analog_custom_stick_forwards(
        self, client, transport
    ):
        """L1 anchor: InputSubscriber.cpp:L94, L242-256 (stick="right").

        send_analog(0.0, 1.0, stick="right") forwards the explicit
        stick value verbatim.
        """
        transport.set_response("input.analog.send", {"ok": True})
        await client.send_analog(0.0, 1.0, stick="right")
        assert transport.calls[-1][0] == "input.analog.send"
        assert transport.calls[-1][1] == {
            "x": 0.0,
            "y": 1.0,
            "stick": "right",
        }
