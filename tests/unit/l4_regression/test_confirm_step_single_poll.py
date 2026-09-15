"""L4 regression tests for V004 (legacy fallback path).

Violation:
- V004 [HIGH]: `_confirm_step_completed` used a double-poll (first wait
  for `stepping` to become False, then wait for it to return True) which
  split the timeout budget in half and could falsely timeout when the
  CPU was already stepping (the first `wait_for_state(stepping is not
  True)` would never satisfy).

Stage 2 fix (commit 778329e): replaced the double-poll with a single
`wait_for_state(stepping is True)` call that uses the full timeout
budget.

Stage 5 redesign (B.2 spec §2.4): the default path is now broadcast
subscription via `transport.wait_for_broadcast("cpu.stepping")`. The
stage-2 single-poll implementation is preserved as
`_confirm_step_completed_legacy` and is reachable via two paths:
  1. `use_broadcast=False` — caller explicitly disables broadcast mode.
  2. Broadcast timeout fallback — degraded path for old PPSSPP builds
     that may not push `cpu.stepping` broadcasts.

This test file anchors the LEGACY fallback path (use_broadcast=False).
The broadcast path is anchored in `test_v004_confirm_step_broadcast.py`.

Anchor:
- L4: `_confirm_step_completed(use_broadcast=False)` invokes
  `wait_for_state` exactly once with the full timeout budget (would
  fail if the legacy path is removed or reverted to double-poll).
- L4: the predicate forwarded to `wait_for_state` is exactly
  `lambda s: s.get("stepping") is True`.
- L4: the return value is the cpu.status state dict.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


class TestV004ConfirmStepLegacySinglePoll:
    """V004 legacy path: _confirm_step_completed(use_broadcast=False).

    These tests pin the stage-2 single-poll behavior that is preserved
    as the fallback for the stage-5 broadcast redesign. The default
    path (use_broadcast=True) is tested in test_v004_confirm_step_broadcast.py.
    """

    @pytest.mark.asyncio
    async def test_confirm_step_uses_single_wait_for_state(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """L4 anchor: wait_for_state is called exactly once (not twice)."""
        transport.wait_for_state = AsyncMock(
            return_value={"stepping": True}
        )
        await client._confirm_step_completed(
            timeout_ms=1000, interval_ms=50, use_broadcast=False
        )
        assert transport.wait_for_state.await_count == 1, (
            "_confirm_step_completed(use_broadcast=False) must call "
            "wait_for_state exactly once — if this fails, the legacy "
            "fallback path was modified or removed."
        )

    @pytest.mark.asyncio
    async def test_confirm_step_predicate_is_stepping_true(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """L4 anchor: forwarded predicate is `s.get('stepping') is True`."""
        transport.wait_for_state = AsyncMock(
            return_value={"stepping": True}
        )
        await client._confirm_step_completed(
            timeout_ms=500, interval_ms=50, use_broadcast=False
        )
        assert transport.wait_for_state.await_count == 1
        captured_predicate = transport.wait_for_state.await_args.args[0]
        # Predicate must accept the cpu.status state dict.
        assert callable(captured_predicate)
        # The V004 contract: predicate matches when stepping is True.
        assert captured_predicate({"stepping": True}) is True, (
            "predicate must return True for stepping=True state"
        )
        # Predicate must NOT match for stepping False / missing / other values.
        assert captured_predicate({"stepping": False}) is False, (
            "predicate must return False for stepping=False state"
        )
        assert captured_predicate({}) is False, (
            "predicate must return False when 'stepping' is absent"
        )
        # `is True` strictly requires bool True, not truthy values like 1.
        assert captured_predicate({"stepping": 1}) is False, (
            "predicate uses `is True` strict identity — must NOT match "
            "non-bool truthy values like 1"
        )

    @pytest.mark.asyncio
    async def test_confirm_step_returns_state_dict(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """L4 anchor: returns the cpu.status dict from wait_for_state."""
        expected = {"stepping": True, "pc": 0x08800000}
        transport.set_state(expected)
        result = await client._confirm_step_completed(
            timeout_ms=500, interval_ms=50, use_broadcast=False
        )
        assert result == expected, (
            "_confirm_step_completed(use_broadcast=False) must return "
            "the cpu.status dict from wait_for_state verbatim"
        )

    @pytest.mark.asyncio
    async def test_confirm_step_no_half_timeout_split(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """L4 anchor: timeout_ms forwarded unchanged (not split in half)."""
        transport.wait_for_state = AsyncMock(
            return_value={"stepping": True}
        )
        await client._confirm_step_completed(
            timeout_ms=2000, interval_ms=50, use_broadcast=False
        )
        assert transport.wait_for_state.await_count == 1
        forwarded_timeout = transport.wait_for_state.await_args.kwargs.get(
            "timeout_ms"
        )
        assert forwarded_timeout == 2000, (
            f"wait_for_state must receive the full timeout_ms=2000, "
            f"got {forwarded_timeout!r} — if this is 1000, the legacy "
            f"fallback was reverted to double-poll (half_timeout restored)."
        )
