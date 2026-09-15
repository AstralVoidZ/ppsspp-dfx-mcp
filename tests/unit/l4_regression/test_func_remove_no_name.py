"""L4 regression tests for V010.

Violation:
- V010 [MEDIUM]: `func_remove` accepted a `name` parameter that does
  not exist in the PPSSPP `hle.func.remove` contract. See
  HLESubscriber.cpp:L38, L319-363 — only `address` (u32, required) is
  registered.

Fix: signature rewritten to `(address)` — `address` is a required
positional parameter, `name` removed entirely.

Anchor:
- L4: `name` NOT in signature; `address` IS required (no default).
- L1: WS event params match `hle.func.remove` contract (no `name`).
"""

from __future__ import annotations

import inspect

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


class TestV010FuncRemoveNoName:
    """V010: func_remove signature must match PPSSPP contract."""

    def test_name_param_removed_from_signature(self):
        """L4 anchor: `name` not in signature."""
        sig = inspect.signature(PpssppDebugClient.func_remove)
        actual_params = set(sig.parameters.keys()) - {"self"}
        assert "name" not in actual_params, (
            "func_remove signature still has `name` param. "
            "If this fails, V010 fix was reverted. See "
            "HLESubscriber.cpp:L38, L319-363."
        )

    def test_address_is_required_positional(self):
        """L4 anchor: `address` is required (no default value)."""
        sig = inspect.signature(PpssppDebugClient.func_remove)
        params = sig.parameters
        assert "address" in params, (
            "func_remove signature missing `address` param."
        )
        assert params["address"].default is inspect.Parameter.empty, (
            f"func_remove `address` should be required (no default), "
            f"got default={params['address'].default!r}"
        )

    @pytest.mark.asyncio
    async def test_positional_call_forwards_address_only(
        self, client, transport
    ):
        """L1 anchor: func_remove(0x08800000) forwards only address."""
        await client.func_remove(0x08800000)
        assert transport.calls[-1][0] == "hle.func.remove"
        assert transport.calls[-1][1] == {"address": 0x08800000}
        # Must NOT contain `name` — contract has no name param.
        assert "name" not in transport.calls[-1][1], (
            "func_remove forwarded `name` to PPSSPP — contract has no "
            "`name` param for hle.func.remove."
        )

    @pytest.mark.asyncio
    async def test_keyword_call_also_works(self, client, transport):
        """L4 anchor: func_remove(address=0x08800000) keyword form works."""
        await client.func_remove(address=0x08800000)
        assert transport.calls[-1][0] == "hle.func.remove"
        assert transport.calls[-1][1] == {"address": 0x08800000}
