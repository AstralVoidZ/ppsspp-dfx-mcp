"""L2: `ppsspp_scan(mode="pattern")` bounds its match count (review C-3).

Before this lock, `max_results` accepted any positive integer while the sibling
value/strings modes already capped at 5000/500. Measured cost of an unbounded
list: 500k hits -> 21.4 MiB response / ~400 ms, so a 256 MiB scan of a
zero-filled region (~134M hits at 2 bytes each) exhausts memory and takes the
whole server down with it.

Two independent layers are pinned here:
  * the wire schema (`maximum`), so a well-behaved client is rejected up front;
  * the engine guard, because `_scan_pattern` is also reachable in-process.
"""

from __future__ import annotations

from typing import Any

import pytest

from ppsspp_dfx_mcp import server as server_mod
from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.service.scan_engine import MAX_SCAN_MATCHES, _scan_pattern


def _max_results_schema() -> dict[str, Any]:
    server_mod.register_all_tools()
    tool = server_mod.mcp._tool_manager._tools["ppsspp_scan"]
    return tool.parameters["properties"]["max_results"]  # type: ignore[index]


class TestWireSchema:
    """The cap is visible to clients in `tools/list`."""

    def test_schema_declares_min_and_max(self) -> None:
        props = _max_results_schema()
        assert props["maximum"] == MAX_SCAN_MATCHES
        assert props["minimum"] == 1

    def test_cap_is_explained_in_the_description(self) -> None:
        """A rejected caller must be told the cap and the way forward."""
        desc = _max_results_schema()["description"]
        assert str(MAX_SCAN_MATCHES) in desc
        assert "narrow" in desc.lower() or "mode='value'" in desc


class TestEngineGuard:
    """Defence in depth: the engine rejects the same input without the SDK."""

    async def test_over_cap_is_rejected(self) -> None:
        with pytest.raises(ArgsInvalid) as exc_info:
            await _scan_pattern("sess", "00", "hex", "0x0", "0x10", MAX_SCAN_MATCHES + 1, 4096)
        assert str(MAX_SCAN_MATCHES) in str(exc_info.value)

    async def test_the_cap_itself_is_accepted(self) -> None:
        """`MAX_SCAN_MATCHES` must pass the cap check (next validation fires)."""
        with pytest.raises(ArgsInvalid) as exc_info:
            await _scan_pattern("sess", "00", "hex", None, None, MAX_SCAN_MATCHES, 4096)
        assert "start_addr" in str(exc_info.value)

    @pytest.mark.parametrize("bad", [0, -1, True])
    async def test_non_positive_and_bool_are_rejected(self, bad: Any) -> None:
        with pytest.raises(ArgsInvalid):
            await _scan_pattern("sess", "00", "hex", "0x0", "0x10", bad, 4096)

    def test_cap_is_aligned_with_the_value_mode(self) -> None:
        """One number for 'how many matches we are willing to hold'."""
        from ppsspp_dfx_mcp.tools.scan import _VALUE_MAX_HITS

        assert MAX_SCAN_MATCHES == _VALUE_MAX_HITS
