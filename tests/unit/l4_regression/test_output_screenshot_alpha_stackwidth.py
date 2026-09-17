"""L4 regression tests for V019.

Violation:
- V019 [MEDIUM]: `_output_screenshot` did not expose `alpha` and
  `stackWidth` parameters. The PPSSPP `gpu.buffer.screenshot` contract
  registers `type` + `alpha` (bool, default false) + `stackWidth`
  (u32, default 0). See GPUBufferSubscriber.cpp:L33, L279-284, L226-260.

Fix: `_output_screenshot` signature extended to
`(alpha=False, stackWidth=0)`; both forwarded to transport.call
alongside `type="uri"`.

Note: `_output_screenshot` is CaptureService's only direct transport
call (architectural exception — must NOT go through DebugClient).

Anchor:
- L4: `alpha` and `stackWidth` in signature with correct defaults.
- L1: WS event params match `gpu.buffer.screenshot` contract.
"""

from __future__ import annotations

import inspect

import pytest
from fake_transport import FakeTransport

from ppsspp_dfx_mcp.service.capture import CaptureService
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


def _configure_stepping(transport: FakeTransport) -> None:
    """Configure FakeTransport faf handlers for with_stepping.

    with_stepping probes cpu.status, then fire_and_forget('cpu.stepping')
    to pause, and fire_and_forget('cpu.resume') to resume. The faf
    handlers update _current_state so wait_for_state returns immediately.
    """
    transport.set_state({"stepping": False})
    transport.set_faf_handler("cpu.stepping", lambda t, **p: t.set_state({"stepping": True}))
    transport.set_faf_handler("cpu.resume", lambda t, **p: t.set_state({"stepping": False}))


@pytest.fixture
def capture_service(transport: FakeTransport) -> CaptureService:
    """CaptureService backed by PpssppDebugClient + FakeTransport.

    Configures stepping faf handlers on the shared transport fixture so
    that with_stepping works inside _output_screenshot.

    V023 (B.2 §4): CaptureService now requires an explicit transport
    parameter. The same FakeTransport that backs the PpssppDebugClient
    is passed explicitly so `_output_screenshot` can call
    `self._transport.call("gpu.buffer.screenshot", ...)` without
    reaching into `_client._transport`.
    """
    _configure_stepping(transport)
    client = PpssppDebugClient(transport)
    return CaptureService(client, transport=transport)


def _screenshot_calls(transport: FakeTransport) -> list[tuple[str, dict]]:
    """Filter transport.calls for gpu.buffer.screenshot invocations."""
    return [(ev, p) for ev, p in transport.calls if ev == "gpu.buffer.screenshot"]


class TestV019OutputScreenshotAlphaStackWidth:
    """V019: _output_screenshot must expose alpha + stackWidth."""

    def test_alpha_and_stackwidth_in_signature_with_defaults(self):
        """L4 anchor: alpha=False and stackWidth=0 in signature."""
        sig = inspect.signature(CaptureService._output_screenshot)
        params = sig.parameters
        assert "alpha" in params, (
            "_output_screenshot missing `alpha` param. V019 fix requires alpha=False default."
        )
        assert "stackWidth" in params, (
            "_output_screenshot missing `stackWidth` param. V019 fix requires stackWidth=0 default."
        )
        assert params["alpha"].default is False, (
            f"`alpha` default should be False, got {params['alpha'].default!r}"
        )
        assert params["stackWidth"].default == 0, (
            f"`stackWidth` default should be 0, got {params['stackWidth'].default!r}"
        )

    @pytest.mark.asyncio
    async def test_default_call_forwards_alpha_false_stackwidth_0(self, capture_service, transport):
        """L1 anchor: default call forwards alpha=False, stackWidth=0."""
        transport.set_response(
            "gpu.buffer.screenshot",
            {"uri": "data:image/png;base64,AAAA"},
        )
        await capture_service._output_screenshot()
        calls = _screenshot_calls(transport)
        assert len(calls) == 1, f"expected 1 gpu.buffer.screenshot call, got {len(calls)}"
        params = calls[0][1]
        assert params["type"] == "uri"
        assert params["alpha"] is False
        assert params["stackWidth"] == 0

    @pytest.mark.asyncio
    async def test_explicit_alpha_true_stackwidth_64_forwards(self, capture_service, transport):
        """L1 anchor: alpha=True, stackWidth=64 forwarded."""
        transport.set_response(
            "gpu.buffer.screenshot",
            {"uri": "data:image/png;base64,AAAA"},
        )
        await capture_service._output_screenshot(alpha=True, stackWidth=64)
        calls = _screenshot_calls(transport)
        assert len(calls) == 1, f"expected 1 gpu.buffer.screenshot call, got {len(calls)}"
        params = calls[0][1]
        assert params["type"] == "uri"
        assert params["alpha"] is True
        assert params["stackWidth"] == 64
