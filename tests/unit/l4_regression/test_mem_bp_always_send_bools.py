"""L4 regression tests for V018.

Violation:
- V018 [MEDIUM]: `mem_bp_add` / `mem_bp_update` used false-omission
  pattern for `read` / `write` / `change` bools (only sent when True).
  PPSSPP contract `memory.breakpoint.add` / `update` registers these
  as OPTIONAL bool params (BreakpointSubscriber.cpp:L286) — absent
  means False, but the false-omission pattern makes it impossible to
  explicitly send `read: False` (e.g. to override a default-True
  mental model). The `enabled` param is always sent; read/write/change
  should follow the same always-sent pattern for consistency.

Fix: always send `read` / `write` / `change` (not just when True).
This also adds `read` / `write` / `change` to `mem_bp_update` (V006
added `size`; V018 adds the three bools on top).

Coordination with V005:
- V005's L4 test `test_change_default_false_omits_param` asserted that
  `change=False` does NOT send the `change` key. V018 flips this to
  always-send, so that test is updated to assert `change: False` IS
  sent (renamed to `test_change_default_false_sends_false`).

Anchor:
- L4: read/write/change always present in forwarded params.
- L1: WS event params match `memory.breakpoint.{add,update}` contract.
"""

from __future__ import annotations

import inspect

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


class TestV018MemBpAddAlwaysSendBools:
    """V018: mem_bp_add always sends read/write/change (not false-omission)."""

    @pytest.mark.asyncio
    async def test_all_false_sends_false_booleans(self, client, transport):
        """L1 anchor: read=False, write=False, change=False → all sent as False.

        V018 flips the false-omission pattern: even False values are
        explicitly forwarded, matching the `enabled` style. This lets
        callers override PPSSPP defaults explicitly.
        """
        await client.mem_bp_add(
            0x08804000, read=False, write=False, change=False
        )
        assert transport.calls[-1][0] == "memory.breakpoint.add"
        params = transport.calls[-1][1]
        assert params["read"] is False, (
            "read=False must be forwarded as `read: False` (V018: always "
            "send, not false-omission). See BreakpointSubscriber.cpp:L286."
        )
        assert params["write"] is False, (
            "write=False must be forwarded as `write: False`."
        )
        assert params["change"] is False, (
            "change=False must be forwarded as `change: False`."
        )

    @pytest.mark.asyncio
    async def test_all_true_sends_true_booleans(self, client, transport):
        """L1 anchor: read=True, write=True, change=True → all sent as True."""
        await client.mem_bp_add(
            0x08804000, read=True, write=True, change=True
        )
        assert transport.calls[-1][0] == "memory.breakpoint.add"
        params = transport.calls[-1][1]
        assert params["read"] is True
        assert params["write"] is True
        assert params["change"] is True

    @pytest.mark.asyncio
    async def test_defaults_send_true_true_false(self, client, transport):
        """L1 anchor: default mem_bp_add() sends read=True, write=True, change=False.

        Defaults are read=True, write=True, change=False — all three are
        always sent (V018), not omitted when False.
        """
        await client.mem_bp_add(0x08804000)
        assert transport.calls[-1][0] == "memory.breakpoint.add"
        params = transport.calls[-1][1]
        assert params["read"] is True, (
            "Default read=True must be forwarded as `read: True`."
        )
        assert params["write"] is True, (
            "Default write=True must be forwarded as `write: True`."
        )
        assert params["change"] is False, (
            "Default change=False must be forwarded as `change: False` "
            "(V018: always send, not false-omission)."
        )


class TestV018MemBpUpdateAlwaysSendBools:
    """V018: mem_bp_update always sends read/write/change when provided.

    V006 added the required `size` param. V018 adds the three optional
    bools `read` / `write` / `change` on top, following the same
    always-send pattern as mem_bp_add.
    """

    def test_signature_has_bool_params(self):
        """L4 anchor: read/write/change are in mem_bp_update signature."""
        sig = inspect.signature(PpssppDebugClient.mem_bp_update)
        for name in ("read", "write", "change"):
            assert name in sig.parameters, (
                f"mem_bp_update must have `{name}` param — if this fails, "
                "V018 fix was reverted. See BreakpointSubscriber.cpp:L286."
            )
            assert sig.parameters[name].default is None, (
                f"mem_bp_update `{name}` should default to None (optional)."
            )

    @pytest.mark.asyncio
    async def test_update_all_false_sends_false_booleans(
        self, client, transport
    ):
        """L1 anchor: mem_bp_update(read=False, write=False, change=False) sent.

        When caller provides read/write/change, they are forwarded as
        explicit booleans (not false-omission). This lets callers
        turn off access flags on an existing breakpoint.
        """
        await client.mem_bp_update(
            address=0x08804000,
            size=4,
            read=False,
            write=False,
            change=False,
        )
        assert transport.calls[-1][0] == "memory.breakpoint.update"
        params = transport.calls[-1][1]
        assert params["read"] is False
        assert params["write"] is False
        assert params["change"] is False

    @pytest.mark.asyncio
    async def test_update_omits_when_none(self, client, transport):
        """L1 anchor: mem_bp_update() without bools does NOT send them.

        The bools default to None (false-omission for mem_bp_update) —
        callers who only want to update `enabled` should not be forced
        to send read/write/change. This differs from mem_bp_add (which
        always sends them) because update is partial/patchy by nature.
        """
        await client.mem_bp_update(address=0x08804000, size=4, enabled=False)
        params = transport.calls[-1][1]
        assert "read" not in params, (
            "Default read=None must NOT be forwarded for update "
            "(partial-update semantics)."
        )
        assert "write" not in params
        assert "change" not in params
