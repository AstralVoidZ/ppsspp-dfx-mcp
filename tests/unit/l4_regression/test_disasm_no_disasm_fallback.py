"""L4 regression tests for V012.

Violation:
- V012 [MEDIUM]: `disasm` had a `disasm` fallback
  (`resp.get("lines") or resp.get("disasm") or []`) that masked
  contract drift. PPSSPP `memory.disasm` returns `range` + `lines`
  + `branchGuides` — there is no `disasm` field. See
  DisasmSubscriber.cpp:L58, L300-380. The fallback could silently
  return [] when PPSSPP renamed the `lines` field.

Fix: removed the `or resp.get("disasm")` fallback. `disasm` now reads
only the `lines` field.

Anchor:
- L1: WS event `memory.disasm` returns `lines` (array of dicts).
- L4: `disasm` source contains NO `disasm` fallback pattern.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


class TestV012DisasmNoDisasmFallback:
    """V012: disasm must read `lines` only (no `disasm` fallback)."""

    @pytest.mark.asyncio
    async def test_disasm_parses_lines_field(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """L1 anchor: `lines` field is returned as a list of dicts."""
        lines = [
            {"text": "jr ra"},
            {"text": "nop"},
        ]
        transport.set_response("memory.disasm", {"lines": lines})

        result = await client.disasm(address=0x08800000, count=2)

        assert result == lines
        assert transport.calls[-1][0] == "memory.disasm"
        assert transport.calls[-1][1] == {
            "address": 0x08800000,
            "count": 2,
        }

    @pytest.mark.asyncio
    async def test_disasm_empty_response_yields_empty_list(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """L1 anchor: missing `lines` returns [] (no fallback to `disasm`).

        If the V012 fix is reverted (re-introducing
        `or resp.get("disasm")`), a response with no `lines` and no
        `disasm` key would still return [] — so this test alone cannot
        catch the revert. The source-level static check below catches it.
        """
        transport.set_response("memory.disasm", {})

        result = await client.disasm(address=0x08800000, count=4)

        assert result == []

    def test_source_has_no_disasm_fallback(self) -> None:
        """L4 anchor: `disasm` source must not contain `disasm` fallback.

        Inspects the live source of `disasm`. If the V012 fix is
        reverted (re-introducing `or resp.get("disasm")` or similar),
        this assertion fails immediately.
        """
        src = inspect.getsource(PpssppDebugClient.disasm)
        assert 'resp.get("disasm"' not in src, (
            "disasm must NOT contain a `resp.get(\"disasm\", ...)` "
            "fallback — V012 fix was reverted. See "
            "DisasmSubscriber.cpp:L58, L300-380 (memory.disasm returns "
            "only the `lines` field, no `disasm` key)."
        )
        assert 'or resp.get("disasm")' not in src, (
            "disasm must NOT contain an `or resp.get(\"disasm\")` "
            "fallback pattern — V012 fix was reverted."
        )
