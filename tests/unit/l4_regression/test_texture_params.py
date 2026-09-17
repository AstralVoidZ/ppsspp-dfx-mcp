"""L4 regression tests for V008.

Violation:
- V008 [CRITICAL]: `texture` passed 6 contract-nonexistent params
  (`address`/`texfmt`/`width`/`height`/`clutaddr`/`clutfmt`) and missed
  `alpha`/`stackWidth`. PPSSPP silently ignores unregistered params, so
  texture capture appeared to work but always captured the currently-
  bound texture (regardless of `address`). See GPUBufferSubscriber.cpp:
  L37, L378-386 — `WebSocketGPUBufferTexture` only registers `level`
  then delegates to `GenericStreamBuffer` (which registers
  `alpha`/`stackWidth`/`type`).

Fix: signature rewritten to `(level=0, output_type="uri", alpha=False,
stackWidth=0)`. The Python-side param is named `output_type` (avoids
shadowing the built-in `type`); it is forwarded to the WS event as
`type` (the PPSSPP protocol name). Docstring explicitly states PPSSPP
captures the currently-bound texture, NOT by VRAM address.

Anchor:
- L4: invalid params (address/texfmt/width/height/clutaddr/clutfmt) NOT
  in signature; valid params (alpha/stackWidth/level/output_type) ARE
  in signature.
- L1: WS event params match `gpu.buffer.texture` contract (forwarded
  under the `type` key).
"""

from __future__ import annotations

import inspect

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

# Params that contract says do NOT exist for gpu.buffer.texture.
_INVALID_TEXTURE_PARAMS = frozenset(
    {
        "address",
        "texfmt",
        "width",
        "height",
        "clutaddr",
        "clutfmt",
        "addr",
        "w",
        "h",
        "clut_addr",
        "clut_fmt",
    }
)

# Params that contract says SHOULD exist for gpu.buffer.texture.
# Python-side name is `output_type` (avoids shadowing built-in `type`);
# forwarded to the WS event as `type`.
# Note: `timeout` is a FakeTransport.call() named param, not a WS param,
# so it never appears in transport.calls[-1][1] (params dict).
_VALID_TEXTURE_PARAMS = frozenset({"level", "output_type", "alpha", "stackWidth"})


class TestV008TextureParamsCleaned:
    """V008: texture signature must match PPSSPP contract."""

    def test_invalid_params_removed_from_signature(self):
        """L4 anchor: none of the 6 invalid params appear in signature."""
        sig = inspect.signature(PpssppDebugClient.texture)
        actual_params = set(sig.parameters.keys()) - {"self"}
        invalid_present = actual_params & _INVALID_TEXTURE_PARAMS
        assert not invalid_present, (
            f"texture signature still has invalid params: {invalid_present}. "
            "If this fails, V008 fix was reverted. See "
            "GPUBufferSubscriber.cpp:L37, L378-386."
        )

    def test_texture_valid_params_present_in_signature(self):
        """L4 anchor: alpha + stackWidth + level + output_type all present."""
        sig = inspect.signature(PpssppDebugClient.texture)
        actual_params = set(sig.parameters.keys()) - {"self"}
        missing = _VALID_TEXTURE_PARAMS - actual_params
        assert not missing, (
            f"texture signature missing valid params: {missing}. "
            "V008 fix requires level/output_type/alpha/stackWidth."
        )

    @pytest.mark.asyncio
    async def test_forwards_only_valid_params_to_ppsspp(self, client, transport):
        """L1 anchor: only contract-valid params sent to gpu.buffer.texture."""
        await client.texture(level=2, output_type="uri", alpha=True, stackWidth=64)
        assert transport.calls[-1][0] == "gpu.buffer.texture"
        # Must NOT contain any of the 6 invalid params.
        sent_keys = set(transport.calls[-1][1].keys())
        invalid_sent = sent_keys & _INVALID_TEXTURE_PARAMS
        assert not invalid_sent, f"texture forwarded invalid params to PPSSPP: {invalid_sent}"
        # Must contain the 4 valid params (WS-side key is `type`).
        params = transport.calls[-1][1]
        assert params["level"] == 2
        assert params["type"] == "uri"
        assert params["alpha"] is True
        assert params["stackWidth"] == 64

    @pytest.mark.asyncio
    async def test_defaults_match_contract(self, client, transport):
        """L1 anchor: default values match PPSSPP contract."""
        await client.texture()
        # PPSSPP defaults: level=0, type="uri", alpha=false, stackWidth=0
        params = transport.calls[-1][1]
        assert params["level"] == 0
        assert params["type"] == "uri"
        assert params["alpha"] is False
        assert params["stackWidth"] == 0
