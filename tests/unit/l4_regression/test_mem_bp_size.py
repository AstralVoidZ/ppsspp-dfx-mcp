"""L4 regression tests for V006 + V007.

Violation:
- V006 [CRITICAL]: `mem_bp_update` missing required `size` param.
  PPSSPP contract `memory.breakpoint.update` requires `address` + `size`
  (BreakpointSubscriber.cpp:L34, L385-397). Without size, PPSSPP fails
  with "Missing 'size' parameter" — mem_bp_update completely broken.
- V007 [CRITICAL]: `mem_bp_remove` missing required `size` param.
  PPSSPP contract `memory.breakpoint.remove` requires `address` + `size`
  (BreakpointSubscriber.cpp:L35, L406-420). Without size, mem_bp_remove
  completely broken.

Fix: Both methods now take `size: int` as a required positional param
and forward it to PPSSPP. Memory breakpoints are matched by address+size
pair (PPSSPP design), so size is needed to identify which breakpoint to
remove/update.

Anchor:
- L4: signature now contains `size` (would fail if reverted).
- L1: WS event params match `memory.breakpoint.{update,remove}` contract.
"""

from __future__ import annotations

import inspect

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


class TestV006MemBpUpdateSize:
    """V006: mem_bp_update must accept and forward `size`."""

    def test_signature_has_size_param(self):
        """L4 anchor: size is in the signature (regression lock)."""
        sig = inspect.signature(PpssppDebugClient.mem_bp_update)
        assert "size" in sig.parameters, (
            "mem_bp_update must have `size` param — if this fails, V006 fix "
            "was reverted. See BreakpointSubscriber.cpp:L34, L385-397."
        )
        # size should NOT have a default (required by contract).
        assert sig.parameters["size"].default is inspect.Parameter.empty, (
            "mem_bp_update `size` should be required (no default) — PPSSPP "
            "contract requires it."
        )

    @pytest.mark.asyncio
    async def test_forwards_size_to_ppsspp(self, client, transport):
        """L1 anchor: size is forwarded to memory.breakpoint.update."""
        await client.mem_bp_update(address=0x08000000, size=4, enabled=False)
        assert transport.calls[-1][0] == "memory.breakpoint.update"
        assert transport.calls[-1][1] == {
            "address": 0x08000000,
            "size": 4,
            "enabled": False,
        }

    @pytest.mark.asyncio
    async def test_size_required_positional(self, client):
        """L4 anchor: calling without size raises TypeError."""
        with pytest.raises(TypeError):
            await client.mem_bp_update(address=0x08000000)  # type: ignore[call-arg]


class TestV007MemBpRemoveSize:
    """V007: mem_bp_remove must accept and forward `size`."""

    def test_signature_has_size_param(self):
        """L4 anchor: size is in the signature (regression lock)."""
        sig = inspect.signature(PpssppDebugClient.mem_bp_remove)
        assert "size" in sig.parameters, (
            "mem_bp_remove must have `size` param — if this fails, V007 fix "
            "was reverted. See BreakpointSubscriber.cpp:L35, L406-420."
        )
        assert sig.parameters["size"].default is inspect.Parameter.empty, (
            "mem_bp_remove `size` should be required (no default) — PPSSPP "
            "contract requires it."
        )

    @pytest.mark.asyncio
    async def test_forwards_size_to_ppsspp(self, client, transport):
        """L1 anchor: size is forwarded to memory.breakpoint.remove."""
        await client.mem_bp_remove(address=0x08000000, size=4)
        assert transport.calls[-1][0] == "memory.breakpoint.remove"
        assert transport.calls[-1][1] == {
            "address": 0x08000000,
            "size": 4,
        }

    @pytest.mark.asyncio
    async def test_size_required_positional(self, client):
        """L4 anchor: calling without size raises TypeError."""
        with pytest.raises(TypeError):
            await client.mem_bp_remove(address=0x08000000)  # type: ignore[call-arg]
