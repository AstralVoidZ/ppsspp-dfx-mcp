"""L1 contract tests for CPU register/evaluate methods.

Anchors:
- cpu.getAllRegs: CPUCoreSubscriber.cpp:L34-37 (thread param optional)
- cpu.getReg: CPUCoreSubscriber.cpp:L35 (not wrapped by DebugClient)
- cpu.setReg: CPUCoreSubscriber.cpp:L36 (name+value+thread)
- cpu.evaluate: CPUCoreSubscriber.cpp:L37 (expression+thread)
"""

from __future__ import annotations

import pytest


class TestCpuRegContract:
    """L1 contract: CPU register methods forward correct event + params."""

    @pytest.mark.asyncio
    async def test_get_all_regs_forwards_event_and_thread(self, client, transport):
        """L1 anchor: get_all_regs(thread=5) forwards cpu.getAllRegs with thread.

        See CPUCoreSubscriber.cpp:L34-37.
        """
        transport.set_response("cpu.getAllRegs", {"categories": []})
        await client.get_all_regs(thread=5)
        assert transport.calls[-1][0] == "cpu.getAllRegs"
        assert transport.calls[-1][1] == {"thread": 5}

    @pytest.mark.asyncio
    async def test_get_all_regs_default_omits_thread(self, client, transport):
        """L1 anchor: get_all_regs() omits thread key (false-omission pattern).

        See CPUCoreSubscriber.cpp:L34-37.
        """
        transport.set_response("cpu.getAllRegs", {"categories": []})
        await client.get_all_regs()
        assert transport.calls[-1][0] == "cpu.getAllRegs"
        assert "thread" not in transport.calls[-1][1]

    @pytest.mark.asyncio
    async def test_get_all_regs_returns_categories(self, client, transport):
        """L1 anchor: get_all_regs returns the response dict with categories.

        See CPUCoreSubscriber.cpp:L34-37 — response contains categories list.
        """
        transport.set_response(
            "cpu.getAllRegs",
            {
                "categories": [
                    {
                        "name": "GPR",
                        "registerNames": ["r0"],
                        "uintValues": [0],
                    }
                ]
            },
        )
        result = await client.get_all_regs()
        assert "categories" in result
        assert result["categories"][0]["name"] == "GPR"

    @pytest.mark.asyncio
    async def test_get_reg_forwards_name_mode(self, client, transport):
        """L1 anchor: get_reg forwards `cpu.getReg` with the register name.

        See CPUCoreSubscriber.cpp:269-323 — `name` mode ("pc"/"hi"/"lo"
        special-cased, otherwise MIPS register names). Response fields:
        {category, register, uintValue, floatValue}.
        """
        transport.set_response(
            "cpu.getReg",
            {"category": 0, "register": 4, "uintValue": 42, "floatValue": "0.000000"},
        )
        result = await client.get_reg("a0")
        assert transport.calls[-1][0] == "cpu.getReg"
        assert transport.calls[-1][1] == {"name": "a0"}
        assert result["uintValue"] == 42

    @pytest.mark.asyncio
    async def test_get_reg_forwards_optional_thread(self, client, transport):
        """L1 anchor: thread is only sent when not None."""
        transport.set_response("cpu.getReg", {"category": 0, "register": 31})
        await client.get_reg("pc", thread=7)
        assert transport.calls[-1][1] == {"name": "pc", "thread": 7}


    @pytest.mark.asyncio
    async def test_set_reg_forwards_event_name_value_thread(self, client, transport):
        """L1 anchor: set_reg forwards cpu.setReg with name+value+thread.

        Pre-set stepping=True so with_stepping becomes a no-op
        (preserve_state=True → skip pause/resume). This isolates the
        setReg call in transport.calls[-1].

        See CPUCoreSubscriber.cpp:L36.
        """
        transport.set_state({"stepping": True})
        transport.set_response("cpu.setReg", {})
        await client.set_reg("r5", 0x100, thread=7)
        set_reg_calls = [
            (ev, p) for ev, p in transport.calls if ev == "cpu.setReg"
        ]
        assert len(set_reg_calls) == 1
        # F-5 fix (2026-09-06): numeric GPR names are translated to the
        # ABI name PPSSPP requires — r5 -> a1 (MIPSDebugInterface.cpp:280).
        assert set_reg_calls[0][1] == {
            "name": "a1",
            "value": 0x100,
            "thread": 7,
        }

    @pytest.mark.asyncio
    async def test_evaluate_forwards_event_and_expression(self, client, transport):
        """L1 anchor: evaluate forwards cpu.evaluate with expression+thread.

        Pre-set stepping=True so with_stepping becomes a no-op.

        See CPUCoreSubscriber.cpp:L37.
        """
        transport.set_state({"stepping": True})
        transport.set_response("cpu.evaluate", {"value": 0x110})
        await client.evaluate("r5 + 0x10", thread=3)
        eval_calls = [
            (ev, p) for ev, p in transport.calls if ev == "cpu.evaluate"
        ]
        assert len(eval_calls) == 1
        assert eval_calls[0][1] == {
            "expression": "r5 + 0x10",
            "thread": 3,
        }
