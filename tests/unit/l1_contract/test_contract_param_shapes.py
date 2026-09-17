"""R10 (design_ppsspp_dfx_mcp_test_refactor_v2 §R10): contract param shapes.

G1 lesson: the contract table declares WHICH events exist (建议1 coverage
test) but nothing verified WHAT parameters the client actually puts on the
wire — the W2 bug (protection check with byte_count=0 while the wire write
carried 4 bytes) was precisely an undeclared shape mismatch between layers.

This sweep calls every PpssppDebugClient method once with representative
arguments (all optionals provided) against a FakeTransport, then diffs the
observed wire param keys against each event's ``params`` declaration:

- observed ⊆ required ∪ optional  → the client never invents parameters
- required ⊆ observed             → every declared required param is sent
- every declared param is observed → declarations don't rot (optional "?"
  params are exercised by the representative calls)

Adding a client param without updating the contract turns this red, and so
does deleting one from the contract.
"""

from __future__ import annotations

import base64 as _b64
from typing import Any

import pytest
from fake_transport import FakeTransport

from ppsspp_dfx_mcp.core.ws_contract import WS_EVENT_CONTRACTS
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


def _zero_page(**kw: Any) -> dict:
    """Callable memory.read response sized to the request (scan-safe)."""
    return {"base64": _b64.b64encode(b"\x00" * kw["size"]).decode("ascii")}


class _TicksAdvancingFake(FakeTransport):
    """cpu.status probes advance ticks so _require_running's 50ms
    progression check sees a running CPU instead of a suspected freeze."""

    async def call(self, event: str, timeout: float = 5.0, **params: Any) -> dict:
        if event == "cpu.status":
            cur = dict(self._current_state)
            self.set_state({**cur, "ticks": cur.get("ticks", 0) + 1000})
        return await super().call(event, timeout=timeout, **params)


@pytest.fixture
def fake() -> FakeTransport:
    t = _TicksAdvancingFake()
    t.set_state({"stepping": False, "pc": 0x08804000, "ticks": 100.0})

    def _stepping_true(t: FakeTransport, **params: Any) -> None:
        t.set_state({**t.state, "stepping": True})

    def _stepping_false(t: FakeTransport, **params: Any) -> None:
        t.set_state({**t.state, "stepping": False})

    _pc = [0x08804000]

    def _advance_and_broadcast(t: FakeTransport, **params: Any) -> None:
        _pc[0] += 4
        t.set_state({**t.state, "pc": _pc[0], "ticks": t.state.get("ticks", 0) + 10})
        t.push_broadcast(
            {
                "event": "cpu.stepping",
                "pc": _pc[0],
                "ticks": t.state.get("ticks", 0),
                "reason": "sweep",
                "relatedAddress": 0,
            }
        )

    t.set_faf_handler("cpu.stepping", _stepping_true)
    t.set_faf_handler("cpu.resume", _stepping_false)
    for ev in ("cpu.stepInto", "cpu.stepOver", "cpu.stepOut", "cpu.runUntil", "cpu.nextHLE"):
        t.set_faf_handler(ev, _advance_and_broadcast)
    t.set_response("memory.read", _zero_page)
    t.set_response(
        "cpu.getAllRegs",
        {
            "categories": [
                {
                    "name": "GPR",
                    "registerNames": ["pc", "a0"],
                    "uintValues": [0x08804000, 0x2A],
                }
            ],
        },
    )
    return t


@pytest.fixture
def client(fake: FakeTransport) -> PpssppDebugClient:
    return PpssppDebugClient(fake)


async def _sweep(client: PpssppDebugClient, fake: FakeTransport) -> dict[str, set[str]]:
    """Call every domain method once; return event → observed param keys."""
    c = client
    # CPU reads/writes (optionals all provided)
    await c.get_reg("a0", thread=1)
    await c.get_all_regs(thread=1)
    await c.set_reg("a0", 1, thread=1)
    await c.evaluate("pc", thread=1)
    # breakpoints (CPU)
    await c.cpu_bp_add(0x1000, enabled=True, condition="a0==1", log=True, log_format="f")
    await c.cpu_bp_list()
    await c.cpu_bp_update(0x1000, enabled=True, log=True, condition="c", log_format="f")
    await c.cpu_bp_remove(0x1000)
    # breakpoints (memory)
    await c.mem_bp_add(
        0x1000,
        size=4,
        read=True,
        write=True,
        change=True,
        enabled=True,
        log=True,
        condition="c",
        log_format="f",
    )
    await c.mem_bp_list()
    await c.mem_bp_update(
        0x1000,
        4,
        enabled=True,
        log=True,
        condition="c",
        log_format="f",
        read=True,
        write=True,
        change=True,
    )
    await c.mem_bp_remove(0x1000, 4)
    # memory
    await c.read_u8(0x1000)
    await c.read_u16(0x1000)
    await c.read_u32(0x1000)
    await c.read_bytes(0x1000, 4)
    await c.read_string(0x1000, max_length=64)
    await c.write_u8(0x1000, 1)
    await c.write_u16(0x1000, 1)
    await c.write_u32(0x1000, 1)
    await c.write_bytes(0x1000, b"\x00\x01")
    await c.scan_memory(b"AB", 0x1000, 0x1100, max_results=10, chunk_size=64)
    await c.search_memory_info("tex", address=0x1000, end=0x2000, type="texture")
    await c.memory_map()
    # disasm
    await c.disasm(0x1000, 4, thread=1)
    await c.assemble(0x1000, "nop")
    await c.search_disasm(0x1000, "jr", end=0x2000, display_symbols=True, thread=1)
    # HLE
    await c.backtrace(thread=1)
    await c.module_list()
    await c.func_list()
    await c.func_scan(0x1000, 16, remove=True)
    await c.func_add("n", 0x1000, 4)
    await c.func_remove(0x1000)
    await c.safe_get_threads()  # hle.thread.list (with_stepping)
    # input
    await c.press_button("cross", 1)
    await c.hold_buttons({"cross": True})
    await c.send_analog(0.1, 0.2, "left")
    # system
    await c.game_status()
    await c.reset(break_=True)
    # GPU
    await c.render_color("uri", True, 8)
    await c.render_depth("uri", True, 8)
    await c.render_stencil("uri", True, 8)
    await c.texture(1, "uri", True, 8)
    await c.clut("uri", True, 8)
    await c.gpu_stats(1.0)
    await c.gpu_record_dump(1.0)
    # replay
    await c.replay_begin()
    await c.replay_status()
    await c.replay_flush()
    await c.replay_execute(1, "AA")
    await c.replay_time_get()
    await c.replay_time_set(7)
    await c.replay_wait_complete(timeout_ms=50, interval_ms=10)
    await c.replay_abort()
    # stepping family (each manages its own pause/resume)
    await c.pause()
    await c.resume()
    await c.safe_get_pc()
    await c.step_into(timeout_ms=2000)
    await c.step_over(timeout_ms=2000)
    await c.step_out(timeout_ms=2000)
    await c.run_until(0x0999, timeout_ms=2000)
    await c.next_hle(timeout_ms=2000)

    observed: dict[str, set[str]] = {}
    for event, params in fake.calls:
        observed.setdefault(event, set()).update(params.keys())
    for event, params in fake.fire_and_forget_calls:
        observed.setdefault(event, set()).update(params.keys())
    return observed


@pytest.mark.asyncio
async def test_wire_param_shapes_match_contract_declarations(
    client: PpssppDebugClient, fake: FakeTransport
) -> None:
    observed = await _sweep(client, fake)
    problems: list[str] = []
    for event, keys in sorted(observed.items()):
        contract = WS_EVENT_CONTRACTS.get(event)
        assert contract is not None, f"event {event} lacks a contract entry"
        declared = set(contract.params)
        required = {p for p in declared if not p.endswith("?")}
        optional = {p[:-1] for p in declared if p.endswith("?")}
        undeclared = keys - required - optional
        if undeclared:
            problems.append(
                f"{event}: wire params {sorted(undeclared)} not declared in "
                f"the contract (client invents parameters)"
            )
        missing_required = required - keys
        if missing_required:
            problems.append(
                f"{event}: declared required params {sorted(missing_required)} "
                f"never observed on the wire"
            )
        unexercised = optional - keys
        if unexercised:
            problems.append(
                f"{event}: declared optional params {sorted(unexercised)} not "
                f"exercised by the sweep (extend the representative call)"
            )
    assert not problems, "\n".join(problems)


@pytest.mark.asyncio
async def test_sweep_covers_every_event_the_client_invokes(
    client: PpssppDebugClient, fake: FakeTransport
) -> None:
    observed = await _sweep(client, fake)
    from tests.unit.l2_mcp_contract.test_ws_contract_coverage import (
        TestSuggest1ContractCoverage,
    )

    invoked = TestSuggest1ContractCoverage._invoked_events()
    missing = sorted(set(invoked) - set(observed))
    assert not missing, f"sweep missed invoked events: {missing}"
