"""L4 regression tests for V013.

Violation:
- V013 [HIGH]: `memory_info_search` docstring claimed the response
  contained a `regions` key (plural, implying a list of matches).
  PPSSPP contract `memory.info.search` actually returns a single
  `extent` field — either null or one object (NOT a list). See
  MemoryInfoSubscriber.cpp:L52, L324-393. The misleading docstring
  led callers to write `result["regions"][0]` and silently break
  when no match was found.

Fix: docstring (and the method-level comment above it) corrected to
describe `extent` (null | single object). The implementation already
forwarded the response verbatim, so no behavior change — only the
contract documentation was wrong.

Anchor:
- L4: docstring mentions `extent`, NOT `regions`.
- L1: WS event name is `memory.info.search`; response `extent` field
  is passed through verbatim.
"""

from __future__ import annotations

import inspect

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


class TestV013MemoryInfoSearchExtent:
    """V013: memory_info_search docstring must describe `extent`, not `regions`."""

    @pytest.mark.asyncio
    async def test_forwards_to_memory_info_search_event(self, client, transport):
        """L1 anchor: call forwards to `memory.info.search` WS event."""
        transport.set_response("memory.info.search", {"extent": None})
        await client.memory_info_search(match="framebuf")
        assert transport.calls[-1][0] == "memory.info.search"
        assert transport.calls[-1][1] == {"match": "framebuf"}

    def test_docstring_says_extent_not_regions(self):
        """L4 anchor: docstring mentions `extent`, not `regions`.

        The V013 violation shipped a docstring that misled callers into
        treating the response as a list under `regions`. The contract
        returns a single `extent` (null | object) — see
        MemoryInfoSubscriber.cpp:L52, L324-393.
        """
        doc = inspect.getdoc(PpssppDebugClient.memory_info_search) or ""
        assert "regions" not in doc, (
            "memory_info_search docstring must NOT mention `regions` "
            "(plural list). If this fails, V013 fix was reverted. See "
            "MemoryInfoSubscriber.cpp:L52, L324-393."
        )
        assert "extent" in doc, (
            "memory_info_search docstring MUST mention `extent` (null | "
            "single object) — see MemoryInfoSubscriber.cpp:L52, L324-393."
        )

    @pytest.mark.asyncio
    async def test_passthrough_extent_field(self, client, transport):
        """L1 anchor: response `extent` field is passed through verbatim.

        PPSSPP returns `{"extent": {...} | null}` — debug_client forwards
        the response dict unchanged. The caller reads `result["extent"]`.
        """
        extent_payload = {
            "type": "texture",
            "address": 0x04000000,
            "size": 0x10000,
            "tag": "framebuf",
        }
        transport.set_response(
            "memory.info.search", {"extent": extent_payload}
        )
        result = await client.memory_info_search(match="framebuf")
        assert "extent" in result
        assert result["extent"] == extent_payload
