"""L4 regression tests for V017.

Violation:
- V017 [MEDIUM]: 6 methods did not expose optional parameters that
  their PPSSPP WS contracts accept. Callers could not use features
  like breakpoint logging, function scan cleanup, GPU buffer alpha
  channel, or analog stick selection.

Fix: add the missing optional parameters with false-omission
forwarding pattern (`if x is not None: params["x"] = x`), except
where the contract requires always-sent defaults.

Affected methods + contracts:
  1. cpu_bp_add: + `log` / `logFormat` (BreakpointSubscriber.cpp:L28, L136-144)
  2. func_add:   + `size` (HLESubscriber.cpp:L37, L240-307)
  3. func_scan:  + `remove` (HLESubscriber.cpp:L41, L483-511)
  4. reset:      + `break` (GameSubscriber.cpp:L26, L41-62) — Python
     param name is `break_` (keyword conflict); WS param name is `break`.
  5. render_color: + `alpha` / `stackWidth`
     (GPUBufferSubscriber.cpp:L33-36, L226-260) — always-sent defaults
     (matches V008/V009 texture/clut pattern).
  6. send_analog: + `stick` (InputSubscriber.cpp:L94, L242-256) — default
     "left", always forwarded.

Note: `read_string` (V001) is handled by another subagent — not tested here.

Anchor:
- L4: signatures contain the new params (would fail if reverted).
- L1: WS event params match each contract.
"""

from __future__ import annotations

import inspect

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

# ============================================================================
# cpu_bp_add: log / logFormat
# ============================================================================


class TestV017CpuBpAddLogParams:
    """V017: cpu_bp_add must accept and forward log / logFormat."""

    def test_signature_has_log_params(self):
        """L4 anchor: `log` and `log_format` are in cpu_bp_add signature."""
        sig = inspect.signature(PpssppDebugClient.cpu_bp_add)
        assert "log" in sig.parameters, (
            "cpu_bp_add must have `log` param — if this fails, V017 fix "
            "was reverted. See BreakpointSubscriber.cpp:L28, L136-144."
        )
        assert "log_format" in sig.parameters, (
            "cpu_bp_add must have `log_format` param (WS name: logFormat)."
        )

    @pytest.mark.asyncio
    async def test_cpu_bp_add_forwards_log_logFormat(self, client, transport):
        """L1 anchor: log=True + log_format=... forwarded to cpu.breakpoint.add."""
        await client.cpu_bp_add(
            address=0x08804000,
            log=True,
            log_format="hit @ {pc}",
        )
        assert transport.calls[-1][0] == "cpu.breakpoint.add"
        params = transport.calls[-1][1]
        assert params["log"] is True, (
            "log=True must be forwarded as `log: True` — see BreakpointSubscriber.cpp:L136-144."
        )
        assert params["logFormat"] == "hit @ {pc}", (
            "log_format must be forwarded as `logFormat` (camelCase) — "
            "see BreakpointSubscriber.cpp:L136-144."
        )

    @pytest.mark.asyncio
    async def test_cpu_bp_add_default_omits_log(self, client, transport):
        """L1 anchor: default cpu_bp_add() does NOT send log / logFormat."""
        await client.cpu_bp_add(address=0x08804000)
        params = transport.calls[-1][1]
        assert "log" not in params, "Default log=None must NOT be forwarded (false-omission)."
        assert "logFormat" not in params, (
            "Default log_format=None must NOT be forwarded (false-omission)."
        )


# ============================================================================
# func_add: size
# ============================================================================


class TestV017FuncAddSize:
    """V017: func_add must accept and forward `size`."""

    def test_signature_has_size(self):
        """L4 anchor: `size` is in func_add signature."""
        sig = inspect.signature(PpssppDebugClient.func_add)
        assert "size" in sig.parameters, (
            "func_add must have `size` param — if this fails, V017 fix "
            "was reverted. See HLESubscriber.cpp:L37, L240-307."
        )
        assert sig.parameters["size"].default is None, (
            "func_add `size` should default to None (optional)."
        )

    @pytest.mark.asyncio
    async def test_func_add_forwards_size(self, client, transport):
        """L1 anchor: func_add(size=0x100) forwards size=0x100 to hle.func.add."""
        await client.func_add(name="my_func", address=0x08804000, size=0x100)
        assert transport.calls[-1][0] == "hle.func.add"
        params = transport.calls[-1][1]
        assert params["size"] == 0x100, (
            "size=0x100 must be forwarded as `size: 0x100` — see HLESubscriber.cpp:L240-307."
        )

    @pytest.mark.asyncio
    async def test_func_add_default_omits_size(self, client, transport):
        """L1 anchor: default func_add() does NOT send size."""
        await client.func_add(name="my_func")
        params = transport.calls[-1][1]
        assert "size" not in params, "Default size=None must NOT be forwarded (false-omission)."


# ============================================================================
# func_scan: remove
# ============================================================================


class TestV017FuncScanRemove:
    """V017: func_scan must accept and forward `remove`."""

    def test_signature_has_remove(self):
        """L4 anchor: `remove` is in func_scan signature."""
        sig = inspect.signature(PpssppDebugClient.func_scan)
        assert "remove" in sig.parameters, (
            "func_scan must have `remove` param — if this fails, V017 fix "
            "was reverted. See HLESubscriber.cpp:L41, L483-511."
        )
        assert sig.parameters["remove"].default is None, (
            "func_scan `remove` should default to None (optional)."
        )

    @pytest.mark.asyncio
    async def test_func_scan_forwards_remove(self, client, transport):
        """L1 anchor: func_scan(remove=True) forwards remove=True to hle.func.scan."""
        await client.func_scan(address=0x08804000, size=0x1000, remove=True)
        assert transport.calls[-1][0] == "hle.func.scan"
        params = transport.calls[-1][1]
        assert params["remove"] is True, (
            "remove=True must be forwarded as `remove: True` — see HLESubscriber.cpp:L483-511."
        )

    @pytest.mark.asyncio
    async def test_func_scan_default_omits_remove(self, client, transport):
        """L1 anchor: default func_scan() does NOT send remove."""
        await client.func_scan(address=0x08804000, size=0x1000)
        params = transport.calls[-1][1]
        assert "remove" not in params, "Default remove=None must NOT be forwarded (false-omission)."


# ============================================================================
# reset: break
# ============================================================================


class TestV017ResetBreak:
    """V017: reset must accept and forward `break` (Python: `break_`)."""

    def test_signature_has_break_(self):
        """L4 anchor: `break_` is in reset signature."""
        sig = inspect.signature(PpssppDebugClient.reset)
        assert "break_" in sig.parameters, (
            "reset must have `break_` param (WS name: break) — if this "
            "fails, V017 fix was reverted. See GameSubscriber.cpp:L26, L41-62."
        )
        assert sig.parameters["break_"].default is None, (
            "reset `break_` should default to None (optional)."
        )

    @pytest.mark.asyncio
    async def test_reset_forwards_break(self, client, transport):
        """L1 anchor: reset(break_=True) forwards `break=True` to game.reset.

        Python `break` is a keyword, so the param name is `break_`;
        the WS event param name is `break` (no underscore).
        """
        await client.reset(break_=True)
        assert transport.calls[-1][0] == "game.reset"
        params = transport.calls[-1][1]
        assert params["break"] is True, (
            "break_=True must be forwarded as `break: True` (WS name) — "
            "see GameSubscriber.cpp:L41-62."
        )

    @pytest.mark.asyncio
    async def test_reset_default_omits_break(self, client, transport):
        """L1 anchor: default reset() does NOT send break."""
        await client.reset()
        params = transport.calls[-1][1]
        assert "break" not in params, "Default break_=None must NOT be forwarded (false-omission)."


# ============================================================================
# render_color: alpha / stackWidth
# ============================================================================


class TestV017RenderBufferParams:
    """V017: render_color/depth/stencil must accept alpha + stackWidth."""

    @pytest.mark.parametrize(
        "method_name",
        ["render_color"],
    )
    def test_signature_has_alpha_stackWidth(self, method_name):
        """L4 anchor: alpha + stackWidth are in each render_* signature."""
        method = getattr(PpssppDebugClient, method_name)
        sig = inspect.signature(method)
        assert "alpha" in sig.parameters, (
            f"{method_name} must have `alpha` param — if this fails, V017 "
            "fix was reverted. See GPUBufferSubscriber.cpp:L33-36, L226-260."
        )
        assert "stackWidth" in sig.parameters, f"{method_name} must have `stackWidth` param."
        assert sig.parameters["alpha"].default is False, (
            f"{method_name} `alpha` should default to False."
        )
        assert sig.parameters["stackWidth"].default == 0, (
            f"{method_name} `stackWidth` should default to 0."
        )

    @pytest.mark.asyncio
    async def test_render_color_forwards_alpha_stackWidth(self, client, transport):
        """L1 anchor: render_color(alpha=True, stackWidth=64) forwarded."""
        await client.render_color(alpha=True, stackWidth=64)
        assert transport.calls[-1][0] == "gpu.buffer.renderColor"
        params = transport.calls[-1][1]
        assert params["alpha"] is True
        assert params["stackWidth"] == 64

    @pytest.mark.asyncio
    async def test_render_color_defaults_sent(self, client, transport):
        """L1 anchor: render_color() sends alpha=False, stackWidth=0.

        Unlike V015's false-omission pattern, render_* methods always
        send alpha/stackWidth (matches V008/V009 texture/clut pattern).
        """
        await client.render_color()
        params = transport.calls[-1][1]
        assert params["alpha"] is False
        assert params["stackWidth"] == 0


# ============================================================================
# send_analog: stick
# ============================================================================


class TestV017SendAnalogStick:
    """V017: send_analog must accept and forward `stick`."""

    def test_signature_has_stick(self):
        """L4 anchor: `stick` is in send_analog signature."""
        sig = inspect.signature(PpssppDebugClient.send_analog)
        assert "stick" in sig.parameters, (
            "send_analog must have `stick` param — if this fails, V017 fix "
            "was reverted. See InputSubscriber.cpp:L94, L242-256."
        )
        assert sig.parameters["stick"].default == "left", (
            "send_analog `stick` should default to 'left'."
        )

    @pytest.mark.asyncio
    async def test_send_analog_forwards_stick_right(self, client, transport):
        """L1 anchor: send_analog(stick='right') forwards stick='right'."""
        await client.send_analog(x=0.5, y=0.5, stick="right")
        assert transport.calls[-1][0] == "input.analog.send"
        params = transport.calls[-1][1]
        assert params["stick"] == "right", (
            "stick='right' must be forwarded as `stick: 'right'` — see "
            "InputSubscriber.cpp:L242-256."
        )

    @pytest.mark.asyncio
    async def test_send_analog_default_stick_left(self, client, transport):
        """L1 anchor: send_analog() forwards stick='left' (default)."""
        await client.send_analog(x=0.0, y=0.0)
        assert transport.calls[-1][0] == "input.analog.send"
        params = transport.calls[-1][1]
        assert params["stick"] == "left", (
            "Default stick='left' must be forwarded as `stick: 'left'`."
        )
