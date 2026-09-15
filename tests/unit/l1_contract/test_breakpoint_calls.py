"""L1 contract tests for breakpoint add/remove/list/update methods.

Anchors:
- cpu.breakpoint.add: BreakpointSubscriber.cpp:L28, L136-144 (enabled+condition+log+logFormat)
- cpu.breakpoint.remove: BreakpointSubscriber.cpp:L29
- cpu.breakpoint.list: BreakpointSubscriber.cpp:L30
- cpu.breakpoint.update: BreakpointSubscriber.cpp:L31
- memory.breakpoint.add: BreakpointSubscriber.cpp:L286 (read+write+change as
  independent optional bools, address+size+enabled required)
- memory.breakpoint.remove: BreakpointSubscriber.cpp (address+size pair)
- memory.breakpoint.list: BreakpointSubscriber.cpp
- memory.breakpoint.update: BreakpointSubscriber.cpp:L286 (read/write/change
  optional partial-update)
"""

from __future__ import annotations

import pytest


class TestCpuBreakpointContract:
    """L1 contract: cpu breakpoint methods forward correct event + params."""

    @pytest.mark.asyncio
    async def test_cpu_bp_add_forwards_required_params(self, client, transport):
        """L1 anchor: cpu_bp_add forwards `cpu.breakpoint.add` with address+enabled.

        Defaults: enabled=True, condition/log/logFormat omitted when None.
        See BreakpointSubscriber.cpp:L28, L136-144.
        """
        await client.cpu_bp_add(0x08804000)
        assert transport.calls[-1][0] == "cpu.breakpoint.add"
        params = transport.calls[-1][1]
        assert params["address"] == 0x08804000
        assert params["enabled"] is True
        # Optional params omitted when None.
        assert "condition" not in params
        assert "log" not in params
        assert "logFormat" not in params

    @pytest.mark.asyncio
    async def test_cpu_bp_remove_forwards_address(self, client, transport):
        """L1 anchor: cpu_bp_remove forwards `cpu.breakpoint.remove` with address.

        See BreakpointSubscriber.cpp:L29.
        """
        await client.cpu_bp_remove(0x08804000)
        assert transport.calls[-1][0] == "cpu.breakpoint.remove"
        assert transport.calls[-1][1] == {"address": 0x08804000}

    @pytest.mark.asyncio
    async def test_cpu_bp_list_forwards_no_params(self, client, transport):
        """L1 anchor: cpu_bp_list forwards `cpu.breakpoint.list` with no params.

        See BreakpointSubscriber.cpp:L30.
        """
        await client.cpu_bp_list()
        assert transport.calls[-1][0] == "cpu.breakpoint.list"
        assert transport.calls[-1][1] == {}

    @pytest.mark.asyncio
    async def test_cpu_bp_update_forwards_address_only(self, client, transport):
        """L1 anchor: cpu_bp_update forwards `cpu.breakpoint.update` with address.

        Optional params (enabled/log/condition/logFormat) omitted when None.
        See BreakpointSubscriber.cpp:L31.
        """
        await client.cpu_bp_update(0x08804000)
        assert transport.calls[-1][0] == "cpu.breakpoint.update"
        params = transport.calls[-1][1]
        assert params == {"address": 0x08804000}
        assert "enabled" not in params
        assert "log" not in params
        assert "condition" not in params
        assert "logFormat" not in params


class TestMemBreakpointContract:
    """L1 contract: memory breakpoint methods forward correct event + params."""

    @pytest.mark.asyncio
    async def test_mem_bp_add_forwards_required_params(self, client, transport):
        """L1 anchor: mem_bp_add forwards `memory.breakpoint.add`.

        Required: address+size+enabled. V018: read/write/change are ALWAYS
        sent as explicit bools (not false-omission). N-02: `log` is also
        ALWAYS sent (default False) to avoid PPSSPP's
        `Result(bool)` reading an uninitialized `log` C++ variable when
        `hasLog=false` — see BreakpointSubscriber.cpp:L309-325. Sending
        log explicitly makes the returned log field consistent with the
        input. See BreakpointSubscriber.cpp:L286.
        """
        await client.mem_bp_add(0x08804000)
        assert transport.calls[-1][0] == "memory.breakpoint.add"
        params = transport.calls[-1][1]
        assert params["address"] == 0x08804000
        assert params["size"] == 4  # default
        assert params["enabled"] is True
        # V018: read/write/change always sent.
        assert params["read"] is True
        assert params["write"] is True
        assert params["change"] is False
        # N-02: log always sent (default False) — fixes the
        # returned log field being inconsistent with input when log
        # was previously omitted.
        assert params["log"] is False
        # Optional params omitted when None.
        assert "condition" not in params
        assert "logFormat" not in params

    @pytest.mark.asyncio
    async def test_mem_bp_add_forwards_log_true_explicitly(self, client, transport):
        """L1 anchor (N-02): mem_bp_add(log=True) forwards log=True.

        Regression: pre-N-02, log=True was forwarded correctly, but log=False
        (the default) was omitted — making the returned `log` field
        inconsistent with input. This test pins the explicit-forward
        contract for log=True so future refactors don't regress to
        conditional-send.
        """
        await client.mem_bp_add(0x08804000, log=True)
        params = transport.calls[-1][1]
        assert params["log"] is True

    @pytest.mark.asyncio
    async def test_mem_bp_remove_forwards_address_and_size(self, client, transport):
        """L1 anchor: mem_bp_remove forwards `memory.breakpoint.remove`.

        Both `address` and `size` are required (memory breakpoints are
        matched by the address+size pair). See BreakpointSubscriber.cpp.
        """
        await client.mem_bp_remove(0x08804000, size=4)
        assert transport.calls[-1][0] == "memory.breakpoint.remove"
        assert transport.calls[-1][1] == {
            "address": 0x08804000,
            "size": 4,
        }

    @pytest.mark.asyncio
    async def test_mem_bp_list_forwards_no_params(self, client, transport):
        """L1 anchor: mem_bp_list forwards `memory.breakpoint.list` with no params.

        See BreakpointSubscriber.cpp.
        """
        await client.mem_bp_list()
        assert transport.calls[-1][0] == "memory.breakpoint.list"
        assert transport.calls[-1][1] == {}

    @pytest.mark.asyncio
    async def test_mem_bp_update_forwards_address_and_size(self, client, transport):
        """L1 anchor: mem_bp_update forwards `memory.breakpoint.update`.

        Required: address+size. Optional params (enabled/log/condition/
        logFormat/read/write/change) omitted when None (partial-update
        semantics — differs from mem_bp_add's always-send). See
        BreakpointSubscriber.cpp:L286.
        """
        await client.mem_bp_update(0x08804000, size=4)
        assert transport.calls[-1][0] == "memory.breakpoint.update"
        params = transport.calls[-1][1]
        assert params == {"address": 0x08804000, "size": 4}
        # Optional partial-update params omitted when None.
        assert "enabled" not in params
        assert "log" not in params
        assert "condition" not in params
        assert "logFormat" not in params
        assert "read" not in params
        assert "write" not in params
        assert "change" not in params
