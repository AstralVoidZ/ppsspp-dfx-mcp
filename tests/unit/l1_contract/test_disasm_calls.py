"""L1 contract tests for memory disasm/assemble/searchDisasm methods.

Anchors:
- memory.disasm: DisasmSubscriber.cpp:L58-59 (address+count+thread, returns lines)
- memory.assemble: DisasmSubscriber.cpp (address+code)
- memory.searchDisasm: DisasmSubscriber.cpp:L58-59 (address+match+displaySymbols+end+thread)
"""

from __future__ import annotations

import pytest


class TestDisasmContract:
    """L1 contract: disasm methods forward correct event + params."""

    @pytest.mark.asyncio
    async def test_disasm_forwards_event_address_count_thread(self, client, transport):
        """L1 anchor: disasm forwards memory.disasm with address+count+thread.

        See DisasmSubscriber.cpp:L58-59.
        """
        transport.set_response("memory.disasm", {"lines": [{"text": "nop"}]})
        await client.disasm(0x08804000, count=4, thread=5)
        assert transport.calls[-1][0] == "memory.disasm"
        assert transport.calls[-1][1] == {
            "address": 0x08804000,
            "count": 4,
            "thread": 5,
        }

    @pytest.mark.asyncio
    async def test_disasm_returns_lines_with_default_count(self, client, transport):
        """L1 anchor: disasm returns the lines list; default count=10.

        See DisasmSubscriber.cpp:L58-59 — response field is `lines`.
        """
        transport.set_response(
            "memory.disasm",
            {"lines": [{"text": "nop"}, {"text": "jr ra"}]},
        )
        result = await client.disasm(0x08804000)
        assert transport.calls[-1][0] == "memory.disasm"
        assert transport.calls[-1][1] == {
            "address": 0x08804000,
            "count": 10,
        }
        assert isinstance(result, list)
        assert result[0]["text"] == "nop"
        assert result[1]["text"] == "jr ra"

    @pytest.mark.asyncio
    async def test_assemble_forwards_event_address_code(self, client, transport):
        """L1 anchor: assemble forwards memory.assemble with address+code.

        See DisasmSubscriber.cpp (memory.assemble contract).
        """
        transport.set_response("memory.assemble", {})
        await client.assemble(0x08804000, "nop")
        assert transport.calls[-1][0] == "memory.assemble"
        assert transport.calls[-1][1] == {
            "address": 0x08804000,
            "code": "nop",
        }

    @pytest.mark.asyncio
    async def test_search_disasm_forwards_event_and_all_params(self, client, transport):
        """L1 anchor: search_disasm forwards memory.searchDisasm with all params.

        end>address triggers forwarding of end. displaySymbols is the
        WS param name (camelCase). thread is forwarded when provided.

        See DisasmSubscriber.cpp:L58-59.
        """
        transport.set_response("memory.searchDisasm", {"lines": []})
        await client.search_disasm(
            0x08804000,
            match="jal",
            end=0x08804100,
            display_symbols=False,
            thread=5,
        )
        assert transport.calls[-1][0] == "memory.searchDisasm"
        assert transport.calls[-1][1] == {
            "address": 0x08804000,
            "match": "jal",
            "displaySymbols": False,
            "end": 0x08804100,
            "thread": 5,
        }
