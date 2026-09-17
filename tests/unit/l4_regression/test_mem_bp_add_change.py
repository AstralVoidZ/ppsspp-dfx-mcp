"""L4 regression tests for V005 (coordinated with V018).

Violation:
- V005 [HIGH]: `mem_bp_add` was missing the `change` parameter.
  PPSSPP contract `memory.breakpoint.add` accepts three independent
  OPTIONAL bool params — `read` / `write` / `change` (registered in
  BreakpointSubscriber.cpp:L286). The `change` flag tracks actual
  value modifications (write-with-same-value is ignored when
  `change=True`). Without this param, callers cannot subscribe to
  change-only breakpoints.

Fix: `change: bool = False` added to `mem_bp_add` signature between
`write` and `enabled`.

V018 coordination: `read`/`write`/`change` are now ALWAYS sent
(not false-omission), so callers can explicitly override PPSSPP
defaults. The assertions below were updated from "absent when False"
to "False is sent" to match the V018 always-send contract.

Anchor:
- L4: signature contains `change` (would fail if reverted).
- L1: WS event params match `memory.breakpoint.add` contract —
  `change` is always forwarded as an explicit bool.
"""

from __future__ import annotations

import inspect

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


class TestV005MemBpAddChange:
    """V005: mem_bp_add must accept and forward `change`."""

    def test_signature_has_change_param(self):
        """L4 anchor: `change` is in the signature (regression lock)."""
        sig = inspect.signature(PpssppDebugClient.mem_bp_add)
        assert "change" in sig.parameters, (
            "mem_bp_add must have `change` param — if this fails, V005 fix "
            "was reverted. See BreakpointSubscriber.cpp:L286."
        )
        # `change` defaults to False (V018 always-send: forwarded as
        # explicit `False` rather than omitted — matches read/write).
        assert sig.parameters["change"].default is False, (
            "mem_bp_add `change` should default to False (V018 always-send "
            "pattern, matching `read`/`write` forwarding semantics)."
        )

    @pytest.mark.asyncio
    async def test_change_default_false_sends_false(self, client, transport):
        """L1 anchor: change=False (default) sends `change: False`.

        V018 coordination: read/write/change are ALWAYS sent (not
        false-omission), so callers can explicitly override PPSSPP
        defaults. The default `change=False` is forwarded as an
        explicit `False` to PPSSPP.
        """
        await client.mem_bp_add(0x08804000, read=True, write=True)
        assert transport.calls[-1][0] == "memory.breakpoint.add"
        params = transport.calls[-1][1]
        assert params["change"] is False, (
            "Default change=False must be forwarded as `change: False` — "
            "V018 always-send contract (callers can override PPSSPP defaults)."
        )

    @pytest.mark.asyncio
    async def test_change_true_forwarded(self, client, transport):
        """L1 anchor: change=True sends `change: True` to PPSSPP."""
        await client.mem_bp_add(0x08804000, read=True, write=True, change=True)
        assert transport.calls[-1][0] == "memory.breakpoint.add"
        params = transport.calls[-1][1]
        assert params["change"] is True, (
            "change=True must be forwarded as `change: True` to PPSSPP — "
            "see BreakpointSubscriber.cpp:L286."
        )

    @pytest.mark.asyncio
    async def test_change_independent_of_read_write(self, client, transport):
        """L1 anchor: change=True works with read=False, write=False.

        PPSSPP registers `read`/`write`/`change` as three INDEPENDENT
        optional bools. A pure change breakpoint (no read, no write)
        must be expressible — see BreakpointSubscriber.cpp:L286.

        V018 coordination: read/write are forwarded as explicit `False`
        (always-send contract), not omitted.
        """
        await client.mem_bp_add(0x08804000, read=False, write=False, change=True)
        assert transport.calls[-1][0] == "memory.breakpoint.add"
        params = transport.calls[-1][1]
        # change must be present and True
        assert params["change"] is True
        # V018: read/write are forwarded as explicit False (always-send)
        assert params["read"] is False
        assert params["write"] is False
