"""L4 regression tests for V009.

Violation:
- V009 [CRITICAL]: `clut` passed 2 contract-nonexistent params
  (`clutaddr`/`clutfmt`) and missed `type`/`alpha`/`stackWidth`. PPSSPP
  silently ignores unregistered params, so clut capture always captured
  the currently-bound CLUT (regardless of `clut_addr`). See
  GPUBufferSubscriber.cpp:L38, L406-412 — `WebSocketGPUBufferClut`
  delegates directly to `GenericStreamBuffer` (which registers
  `alpha`/`stackWidth`/`type`), no address params at all.

Fix: signature rewritten to `(output_type="uri", alpha=False,
stackWidth=0)`. The Python-side param is named `output_type` (avoids
shadowing the built-in `type`); it is forwarded to the WS event as
`type` (the PPSSPP protocol name). Docstring explicitly states PPSSPP
captures the currently-bound CLUT, NOT by VRAM address.

Anchor:
- L4: invalid params (clutaddr/clutfmt/clut_addr/clut_fmt) NOT in
  signature; valid params (output_type/alpha/stackWidth) ARE in
  signature.
- L1: WS event params match `gpu.buffer.clut` contract (forwarded
  under the `type` key).
"""

from __future__ import annotations

import inspect

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


_INVALID_CLUT_PARAMS = frozenset(
    {"clutaddr", "clutfmt", "clut_addr", "clut_fmt", "address"}
)

_VALID_CLUT_PARAMS = frozenset({"output_type", "alpha", "stackWidth"})


class TestV009ClutParamsCleaned:
    """V009: clut signature must match PPSSPP contract."""

    def test_invalid_params_removed_from_signature(self):
        """L4 anchor: clutaddr/clutfmt not in signature."""
        sig = inspect.signature(PpssppDebugClient.clut)
        actual_params = set(sig.parameters.keys()) - {"self"}
        invalid_present = actual_params & _INVALID_CLUT_PARAMS
        assert not invalid_present, (
            f"clut signature still has invalid params: {invalid_present}. "
            "If this fails, V009 fix was reverted. See "
            "GPUBufferSubscriber.cpp:L38, L406-412."
        )

    def test_clut_valid_params_present_in_signature(self):
        """L4 anchor: output_type + alpha + stackWidth all present."""
        sig = inspect.signature(PpssppDebugClient.clut)
        actual_params = set(sig.parameters.keys()) - {"self"}
        missing = _VALID_CLUT_PARAMS - actual_params
        assert not missing, (
            f"clut signature missing valid params: {missing}. "
            "V009 fix requires output_type/alpha/stackWidth."
        )

    @pytest.mark.asyncio
    async def test_forwards_only_valid_params_to_ppsspp(self, client, transport):
        """L1 anchor: only contract-valid params sent to gpu.buffer.clut."""
        await client.clut(output_type="base64", alpha=True, stackWidth=32)
        assert transport.calls[-1][0] == "gpu.buffer.clut"
        sent_keys = set(transport.calls[-1][1].keys())
        invalid_sent = sent_keys & _INVALID_CLUT_PARAMS
        assert not invalid_sent, (
            f"clut forwarded invalid params to PPSSPP: {invalid_sent}"
        )
        params = transport.calls[-1][1]
        assert params["type"] == "base64"
        assert params["alpha"] is True
        assert params["stackWidth"] == 32

    @pytest.mark.asyncio
    async def test_defaults_match_contract(self, client, transport):
        """L1 anchor: default values match PPSSPP contract."""
        await client.clut()
        # PPSSPP defaults (via GenericStreamBuffer): type="uri",
        # alpha=false, stackWidth=0
        params = transport.calls[-1][1]
        assert params["type"] == "uri"
        assert params["alpha"] is False
        assert params["stackWidth"] == 0

    @pytest.mark.asyncio
    async def test_no_required_params(self, client, transport):
        """L4 anchor: clut takes no required params (all optional)."""
        # clut() should be callable with no args — all params optional.
        await client.clut()
        assert transport.calls[-1][0] == "gpu.buffer.clut"
