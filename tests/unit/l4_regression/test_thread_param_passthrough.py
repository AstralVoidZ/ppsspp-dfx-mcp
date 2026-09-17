"""L4 regression tests for V015.

Violation:
- V015 [MEDIUM]: 5 native CPU/disasm methods did not expose the
  `thread` (u32, optional) parameter that PPSSPP contracts accept
  for per-thread queries. Affected events (with source anchors):
    - `cpu.getAllRegs` (CPUCoreSubscriber.cpp:L34-37)
    - `cpu.getReg`    (CPUCoreSubscriber.cpp:L34-37)
    - `cpu.setReg`    (CPUCoreSubscriber.cpp:L34-37)
    - `cpu.evaluate`  (CPUCoreSubscriber.cpp:L34-37)
    - `memory.disasm` (DisasmSubscriber.cpp:L58-59)
    - `memory.searchDisasm` (DisasmSubscriber.cpp:L58-59)
    - `hle.backtrace` (HLESubscriber.cpp:L43) — already exposed; reference
  Without `thread`, callers cannot query register/disasm state for a
  non-current thread.

Fix: add `thread: Optional[int] = None` to the 5 native methods
(`get_all_regs`, `set_reg`, `evaluate`, `disasm`, `search_disasm`)
and forward via the `if thread is not None: params["thread"] = thread`
pattern. `backtrace` already follows this pattern (reference).

Anchor:
- L4: signature contains `thread` (would fail if reverted).
- L1: WS event params include `thread` only when explicitly provided.
"""

from __future__ import annotations

import inspect

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


class TestV015ThreadParamPassthrough:
    """V015: 5 native methods must accept and forward `thread`."""

    # -------------------- get_all_regs --------------------

    def test_get_all_regs_signature_has_thread(self):
        """L4 anchor: `thread` is in get_all_regs signature."""
        sig = inspect.signature(PpssppDebugClient.get_all_regs)
        assert "thread" in sig.parameters, (
            "get_all_regs must have `thread` param — if this fails, V015 "
            "fix was reverted. See CPUCoreSubscriber.cpp:L34-37."
        )
        assert sig.parameters["thread"].default is None, (
            "get_all_regs `thread` should default to None (optional)."
        )

    @pytest.mark.asyncio
    async def test_get_all_regs_forwards_thread(self, client, transport):
        """L1 anchor: get_all_regs(thread=5) forwards thread=5 to cpu.getAllRegs."""
        await client.get_all_regs(thread=5)
        assert transport.calls[-1][0] == "cpu.getAllRegs"
        params = transport.calls[-1][1]
        assert params["thread"] == 5, (
            "thread=5 must be forwarded as `thread: 5` to PPSSPP — see "
            "CPUCoreSubscriber.cpp:L34-37."
        )

    @pytest.mark.asyncio
    async def test_get_all_regs_default_no_thread(self, client, transport):
        """L1 anchor: get_all_regs() does NOT send thread key."""
        await client.get_all_regs()
        assert transport.calls[-1][0] == "cpu.getAllRegs"
        params = transport.calls[-1][1]
        assert "thread" not in params, (
            "Default thread=None must NOT be forwarded to PPSSPP — "
            "false-omission pattern (matches backtrace)."
        )

    # -------------------- set_reg --------------------

    def test_set_reg_signature_has_thread(self):
        """L4 anchor: `thread` is in set_reg signature."""
        sig = inspect.signature(PpssppDebugClient.set_reg)
        assert "thread" in sig.parameters, (
            "set_reg must have `thread` param — if this fails, V015 fix "
            "was reverted. See CPUCoreSubscriber.cpp:L34-37."
        )
        assert sig.parameters["thread"].default is None, (
            "set_reg `thread` should default to None (optional)."
        )

    @pytest.mark.asyncio
    async def test_set_reg_forwards_thread(self, client, transport):
        """L1 anchor: set_reg(thread=5) forwards thread=5 to cpu.setReg.

        Pre-set stepping=True so with_stepping becomes a no-op
        (preserve_state=True → skip pause/resume). This isolates the
        setReg call in transport.calls[-1].
        """
        transport.set_state({"stepping": True})
        await client.set_reg("r5", 0x100, thread=5)
        # Find the cpu.setReg call (with_stepping also calls cpu.status).
        set_reg_calls = [(ev, p) for ev, p in transport.calls if ev == "cpu.setReg"]
        assert len(set_reg_calls) == 1
        params = set_reg_calls[0][1]
        assert params["thread"] == 5, (
            "thread=5 must be forwarded as `thread: 5` to PPSSPP — see "
            "CPUCoreSubscriber.cpp:L34-37."
        )

    # -------------------- evaluate --------------------

    def test_evaluate_signature_has_thread(self):
        """L4 anchor: `thread` is in evaluate signature."""
        sig = inspect.signature(PpssppDebugClient.evaluate)
        assert "thread" in sig.parameters, (
            "evaluate must have `thread` param — if this fails, V015 fix "
            "was reverted. See CPUCoreSubscriber.cpp:L34-37."
        )
        assert sig.parameters["thread"].default is None, (
            "evaluate `thread` should default to None (optional)."
        )

    @pytest.mark.asyncio
    async def test_evaluate_forwards_thread(self, client, transport):
        """L1 anchor: evaluate(thread=5) forwards thread=5 to cpu.evaluate."""
        transport.set_state({"stepping": True})
        await client.evaluate("r5 + 0x10", thread=5)
        eval_calls = [(ev, p) for ev, p in transport.calls if ev == "cpu.evaluate"]
        assert len(eval_calls) == 1
        params = eval_calls[0][1]
        assert params["thread"] == 5, (
            "thread=5 must be forwarded as `thread: 5` to PPSSPP — see "
            "CPUCoreSubscriber.cpp:L34-37."
        )

    # -------------------- disasm --------------------

    def test_disasm_signature_has_thread(self):
        """L4 anchor: `thread` is in disasm signature."""
        sig = inspect.signature(PpssppDebugClient.disasm)
        assert "thread" in sig.parameters, (
            "disasm must have `thread` param — if this fails, V015 fix "
            "was reverted. See DisasmSubscriber.cpp:L58-59."
        )
        assert sig.parameters["thread"].default is None, (
            "disasm `thread` should default to None (optional)."
        )

    @pytest.mark.asyncio
    async def test_disasm_forwards_thread(self, client, transport):
        """L1 anchor: disasm(thread=5) forwards thread=5 to memory.disasm."""
        await client.disasm(0x08804000, count=4, thread=5)
        assert transport.calls[-1][0] == "memory.disasm"
        params = transport.calls[-1][1]
        assert params["thread"] == 5, (
            "thread=5 must be forwarded as `thread: 5` to PPSSPP — see DisasmSubscriber.cpp:L58-59."
        )

    @pytest.mark.asyncio
    async def test_disasm_default_no_thread(self, client, transport):
        """L1 anchor: disasm() does NOT send thread key."""
        await client.disasm(0x08804000, count=4)
        assert transport.calls[-1][0] == "memory.disasm"
        params = transport.calls[-1][1]
        assert "thread" not in params, "Default thread=None must NOT be forwarded to PPSSPP."

    # -------------------- search_disasm --------------------

    def test_search_disasm_signature_has_thread(self):
        """L4 anchor: `thread` is in search_disasm signature."""
        sig = inspect.signature(PpssppDebugClient.search_disasm)
        assert "thread" in sig.parameters, (
            "search_disasm must have `thread` param — if this fails, V015 "
            "fix was reverted. See DisasmSubscriber.cpp:L58-59."
        )
        assert sig.parameters["thread"].default is None, (
            "search_disasm `thread` should default to None (optional)."
        )

    @pytest.mark.asyncio
    async def test_search_disasm_forwards_thread(self, client, transport):
        """L1 anchor: search_disasm(thread=5) forwards thread=5 to memory.searchDisasm."""
        await client.search_disasm(0x08804000, match="jal", thread=5)
        assert transport.calls[-1][0] == "memory.searchDisasm"
        params = transport.calls[-1][1]
        assert params["thread"] == 5, (
            "thread=5 must be forwarded as `thread: 5` to PPSSPP — see DisasmSubscriber.cpp:L58-59."
        )

    @pytest.mark.asyncio
    async def test_search_disasm_default_no_thread(self, client, transport):
        """L1 anchor: search_disasm() does NOT send thread key."""
        await client.search_disasm(0x08804000, match="jal")
        assert transport.calls[-1][0] == "memory.searchDisasm"
        params = transport.calls[-1][1]
        assert "thread" not in params, "Default thread=None must NOT be forwarded to PPSSPP."
