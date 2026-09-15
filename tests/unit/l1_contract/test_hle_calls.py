"""L1 contract tests for HLE thread/module/function methods.

Anchors:
- hle.backtrace: HLESubscriber.cpp:L43 (thread optional)
- hle.module.list: HLESubscriber.cpp:L43 (no params)
- hle.func.list: HLESubscriber.cpp:L43 (no params)
- hle.func.scan: HLESubscriber.cpp:L41, L483-511 (address+size+remove)
- hle.func.add: HLESubscriber.cpp:L37, L240-307 (name+address+size)
- hle.func.remove: HLESubscriber.cpp:L38, L319-363 (address only)
"""

from __future__ import annotations

import pytest


class TestHleBacktraceContract:
    """L1 contract: hle.backtrace forwards event + optional thread."""

    @pytest.mark.asyncio
    async def test_backtrace_default_omits_thread(self, client, transport):
        """L1 anchor: backtrace() omits thread key (false-omission pattern).

        See HLESubscriber.cpp:L43.
        """
        transport.set_response("hle.backtrace", {"frames": []})
        await client.backtrace()
        assert transport.calls[-1][0] == "hle.backtrace"
        assert "thread" not in transport.calls[-1][1]

    @pytest.mark.asyncio
    async def test_backtrace_forwards_thread(self, client, transport):
        """L1 anchor: backtrace(thread=5) forwards thread=5.

        See HLESubscriber.cpp:L43.
        """
        transport.set_response("hle.backtrace", {"frames": []})
        await client.backtrace(thread=5)
        assert transport.calls[-1][0] == "hle.backtrace"
        assert transport.calls[-1][1] == {"thread": 5}


class TestHleModuleFuncContract:
    """L1 contract: HLE module/func methods forward correct event + params."""

    @pytest.mark.asyncio
    async def test_module_list_forwards_event_no_params(self, client, transport):
        """L1 anchor: module_list forwards hle.module.list with no params.

        See HLESubscriber.cpp:L43.
        """
        transport.set_response("hle.module.list", {"modules": []})
        await client.module_list()
        assert transport.calls[-1][0] == "hle.module.list"
        assert transport.calls[-1][1] == {}

    @pytest.mark.asyncio
    async def test_func_list_forwards_event_no_params(self, client, transport):
        """L1 anchor: func_list forwards hle.func.list with no params.

        See HLESubscriber.cpp:L43.
        """
        transport.set_response("hle.func.list", {"functions": []})
        await client.func_list()
        assert transport.calls[-1][0] == "hle.func.list"
        assert transport.calls[-1][1] == {}

    @pytest.mark.asyncio
    async def test_func_scan_forwards_address_size_remove(self, client, transport):
        """L1 anchor: func_scan forwards hle.func.scan with address+size+remove.

        See HLESubscriber.cpp:L41, L483-511.
        """
        transport.set_response("hle.func.scan", {})
        await client.func_scan(0x08804000, 0x100, remove=True)
        assert transport.calls[-1][0] == "hle.func.scan"
        assert transport.calls[-1][1] == {
            "address": 0x08804000,
            "size": 0x100,
            "remove": True,
        }

    @pytest.mark.asyncio
    async def test_func_add_forwards_name_address_size(self, client, transport):
        """L1 anchor: func_add forwards hle.func.add with name+address+size.

        See HLESubscriber.cpp:L37, L240-307.
        """
        transport.set_response("hle.func.add", {})
        await client.func_add(name="my_func", address=0x08804000, size=0x40)
        assert transport.calls[-1][0] == "hle.func.add"
        assert transport.calls[-1][1] == {
            "name": "my_func",
            "address": 0x08804000,
            "size": 0x40,
        }

    @pytest.mark.asyncio
    async def test_func_remove_forwards_address_only(self, client, transport):
        """L1 anchor: func_remove forwards hle.func.remove with address only.

        See HLESubscriber.cpp:L38, L319-363 — only address is registered.
        """
        transport.set_response("hle.func.remove", {})
        await client.func_remove(0x08804000)
        assert transport.calls[-1][0] == "hle.func.remove"
        assert transport.calls[-1][1] == {"address": 0x08804000}
