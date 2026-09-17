"""Real-PPSSPP lock: memory watchpoints with size > 4 are range watches.

Blind-eval verification V1 (2026-09-14, PPSSPP v1.20.4-605): a size=8
watchpoint registers, lists with the requested address+size, and is
removed by the address+size pair. This is why ``mem_set.size`` stays a
plain ``int`` (not ``Literal[1, 2, 4]``) — sizes above the word size are
a real PPSSPP capability, not an input mistake.

Requires a real PPSSPP + ISO (auto-skips when absent).
"""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

pytestmark = [pytest.mark.real_ppsspp, pytest.mark.integration]


class TestMemBreakpointRangeWatch:
    async def test_size8_range_watch_roundtrip(self, real_transport):
        client = PpssppDebugClient(real_transport)
        addr = 0x09000000  # user data — outside kernel / top.prx ranges

        # PPSSPP's add response is a bare receipt (event/ticket only) —
        # the registration is verified via the list round-trip below.
        await client.mem_bp_add(addr, size=8, read=True, write=True)

        listed = await client.mem_bp_list()
        entries = [bp for bp in listed.get("breakpoints", []) if bp.get("address") == addr]
        assert len(entries) == 1, f"expected exactly one watch at {addr:#x}"
        assert entries[0].get("size") == 8

        removed = await client.mem_bp_remove(addr, size=8)
        assert not removed.get("breakpoints"), f"watch not removed: {removed}"
