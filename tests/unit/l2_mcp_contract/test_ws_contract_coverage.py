"""P5 review-fix tests (2026-09-06).

- W10a: any ConnectionError (not just ConnectionRefusedError) translates
  to WsDisconnected — the recv loop fails pending futures with plain
  ConnectionError("PPSSPP WebSocket disconnected"), which used to surface
  as generic INTERNAL.
- W10b: StepNoAdvanceError passes through to_tool_error unchanged (it is
  a ToolError with code STEP_NO_ADVANCE) — the F-4 no-advance diagnosis
  used to be misclassified as WsDisconnected with a "reconnect" hint.
- 建议1: every WS event invoked by PpssppDebugClient must have a
  WS_EVENT_CONTRACTS entry (the invariant ws_contract.py itself declares).
  This test would have caught the missing cpu.getReg entry.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from ppsspp_dfx_mcp.core import stepping as stepping_module
from ppsspp_dfx_mcp.core.ws_contract import WS_EVENT_CONTRACTS, get_contract
from ppsspp_dfx_mcp.errors import (
    StepNoAdvanceError,
    WsDisconnected,
    to_tool_error,
)
from ppsspp_dfx_mcp.service import debug_client as dc_module


class TestW10aConnectionErrorClassification:
    def test_plain_connection_error_maps_to_ws_disconnected(self):
        exc = ConnectionError("PPSSPP WebSocket disconnected")
        translated = to_tool_error(exc)
        assert isinstance(translated, WsDisconnected), (
            "W10a: a recv-loop disconnect (plain ConnectionError) must be "
            "classified as WsDisconnected, not generic INTERNAL"
        )

    def test_connection_reset_maps_to_ws_disconnected(self):
        assert isinstance(to_tool_error(ConnectionResetError()), WsDisconnected)

    def test_connection_refused_still_maps_to_ws_disconnected(self):
        assert isinstance(to_tool_error(ConnectionRefusedError()), WsDisconnected)


class TestW10bStepNoAdvancePassthrough:
    def test_step_no_advance_is_tool_error_passthrough(self):
        exc = StepNoAdvanceError(
            "step did not advance the CPU after 3 attempt(s) — use a "
            "breakpoint or step action=run_until instead"
        )
        translated = to_tool_error(exc)
        assert translated is exc, "W10b: the no-advance diagnosis must reach the client verbatim"
        assert translated.code == "STEP_NO_ADVANCE"

    def test_message_not_polluted_with_reconnect_hint(self):
        exc = StepNoAdvanceError("step did not advance the CPU")
        text = str(to_tool_error(exc))
        assert "reconnect" not in text.lower(), (
            "W10b: the no-advance error must not carry the WsDisconnected "
            "'reconnect' hint that misled agents"
        )


class TestSuggest1ContractCoverage:
    """Every event PpssppDebugClient sends must be in WS_EVENT_CONTRACTS.

    Static extraction of ``self._transport.call("<event>")`` and
    ``self._transport.fire_and_forget("<event>")`` literals from the
    debug_client module source (plus SteppingManager's cpu.stepping /
    cpu.resume / cpu.status, issued via the same transport protocol).
    """

    @staticmethod
    def _invoked_events() -> set[str]:
        events: set[str] = set()
        files = [
            Path(inspect.getfile(dc_module)),
            Path(inspect.getfile(stepping_module)),
        ]
        for path in files:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                # Bound-method call sites: self._transport.call("event", ...)
                # / self._transport.fire_and_forget("event", ...) — the
                # event name is the first positional argument in both.
                # _step_with_retry("cpu.stepInto"|"cpu.stepOver"|
                # "cpu.stepOut", ...) fires the event internally.
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr in ("call", "fire_and_forget", "_step_with_retry")
                ):
                    continue
                if not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    events.add(first.value)
        return events

    def test_debug_client_events_have_contract_entries(self):
        events = self._invoked_events()
        assert events, "extraction must find at least the known events"
        missing = sorted(events - set(WS_EVENT_CONTRACTS))
        assert not missing, (
            f"WS events invoked by the client/stepping layer but missing "
            f"from WS_EVENT_CONTRACTS: {missing}"
        )

    def test_known_anchors_covered(self):
        events = self._invoked_events()
        for anchor in ("cpu.getReg", "cpu.getAllRegs", "cpu.stepInto", "memory.read"):
            assert anchor in events, f"extraction lost anchor {anchor!r}"

    def test_get_contract_works_for_every_invoked_event(self):
        for event in self._invoked_events():
            contract = get_contract(event)
            assert contract.event == event
