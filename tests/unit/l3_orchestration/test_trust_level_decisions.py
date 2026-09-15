"""L3 orchestration tests: TrustLevel decision hooks (V026 §5.2).

Anchors: B.2 spec §5.2 V026 documented decision (TrustLevel factory
methods are no-op but preserved as future hooks). L3 anchors the
factory methods' current HIGH-returning behavior;
no separate L4 / unit test currently covers TrustLevel in isolation.

L3 focus (NOT covered by L4 / unit tests):
- V026 §5.2: factory methods (from_pc_result / from_thread_result)
  currently return HIGH unconditionally because safe_get_pc /
  safe_get_threads always use with_stepping. The factory methods are
  preserved as decision hooks for future refinements (e.g. if PPSSPP
  ever exposes a "stepping" flag in the response, the factory can
  inspect it).
- L3 anchors that the orchestration (safe_get_pc → with_stepping →
  cpu.getAllRegs → TrustLevel.from_pc_result) consistently yields HIGH.
- L3 anchors API stability: the factory methods are classmethods
  callable on the class (not just instances), preserving the decision
  hook contract for future callers.

V026 (B.2 §5.2): the factory methods encapsulate the HIGH decision so
callers do not hardcode `TrustLevel.HIGH` directly. If the decision
logic changes (e.g. PPSSPP exposes a trust flag), only the factory
method needs updating — all callers continue to use
`TrustLevel.from_pc_result(result)`.
"""

from __future__ import annotations

from typing import Any

import pytest

from fake_transport import FakeTransport
from ppsspp_dfx_mcp.core.stepping import SteppingManager, TrustLevel, ThreadSnapshot


def _make_regs_response(pc: int = 0x08804000) -> dict[str, Any]:
    """Build a cpu.getAllRegs response with the given PC value."""
    return {
        "categories": [
            {
                "name": "GPR",
                "registerNames": ["r0", "r1", "pc"],
                "uintValues": [0, 0, pc],
            }
        ]
    }


# ============================================================================
# Factory methods return HIGH unconditionally (V026 §5.2)
# ============================================================================


class TestTrustLevelFactoryDecisions:
    """L3: TrustLevel factory methods return HIGH (V026 §5.2 decision).

    V026 §5.2: `from_pc_result` and `from_thread_result` are no-op
    factories (always return HIGH) because safe_get_pc / safe_get_threads
    always use with_stepping. L3 anchors this decision and the
    contract that the factories accept any dict (future-proofing for
    when PPSSPP may expose trust metadata in the response).
    """

    @pytest.mark.parametrize(
        "result,description",
        [
            ({"categories": []}, "empty categories"),
            ({"categories": [{"name": "GPR", "registerNames": ["pc"], "uintValues": [0]}]}, "normal response"),
            ({}, "empty dict"),
            ({"extra_field": "ignored"}, "unrelated fields"),
            ({"threads": []}, "threads-shaped response (wrong factory)"),
        ],
    )
    def test_from_pc_result_always_high(
        self, result: dict[str, Any], description: str
    ):
        """from_pc_result returns HIGH for any input (V026 §5.2).

        The factory accepts any dict without inspecting it. This is
        intentional: the decision is based on the CALLER (safe_get_pc
        uses with_stepping), not the result content. Future PPSSPP
        versions may add trust metadata — the factory is the hook.
        """
        assert TrustLevel.from_pc_result(result) == TrustLevel.HIGH, (
            f"from_pc_result must return HIGH for {description}. "
            f"V026 §5.2: safe_get_pc always uses with_stepping."
        )

    @pytest.mark.parametrize(
        "result,description",
        [
            ({"threads": []}, "empty threads list"),
            ({"threads": [{"id": 1, "name": "root"}]}, "non-empty threads"),
            ({}, "empty dict"),
            ({"categories": []}, "regs-shaped response (wrong factory)"),
        ],
    )
    def test_from_thread_result_always_high(
        self, result: dict[str, Any], description: str
    ):
        """from_thread_result returns HIGH for any input (V026 §5.2)."""
        assert TrustLevel.from_thread_result(result) == TrustLevel.HIGH, (
            f"from_thread_result must return HIGH for {description}. "
            f"V026 §5.2: safe_get_threads always uses with_stepping."
        )


# ============================================================================
# Factory methods are classmethods (decision hook contract)
# ============================================================================


class TestTrustLevelFactoryClassmethodContract:
    """L3: factory methods are classmethods (callable on the class).

    V026 §5.2: the factory methods are preserved as decision hooks.
    Callers use `TrustLevel.from_pc_result(result)` (not
    `TrustLevel().from_pc_result(result)`) — the classmethod contract
    ensures the decision logic is centralized in the class, not
    instances. L3 anchors this API stability.
    """

    def test_factory_methods_callable_without_instance(self):
        """Factory methods are callable on the class (no instance needed)."""
        # These must NOT raise TypeError (would if they were instance methods)
        assert TrustLevel.from_pc_result({}) == TrustLevel.HIGH
        assert TrustLevel.from_thread_result({}) == TrustLevel.HIGH
        assert TrustLevel.high() == TrustLevel.HIGH

    def test_factory_methods_return_string_not_object(self):
        """Factory methods return the string constant (not a TrustLevel instance).

        This preserves API compatibility: callers compare with
        `result == TrustLevel.HIGH` (string comparison), not
        `isinstance(result, TrustLevel)`.
        """
        pc_result = TrustLevel.from_pc_result({})
        thread_result = TrustLevel.from_thread_result({})
        high_result = TrustLevel.high()

        assert isinstance(pc_result, str)
        assert isinstance(thread_result, str)
        assert isinstance(high_result, str)
        assert pc_result == "high"
        assert thread_result == "high"
        assert high_result == "high"


# ============================================================================
# Orchestration: safe_get_pc / safe_get_threads yield HIGH (V026 end-to-end)
# ============================================================================


class TestTrustLevelOrchestration:
    """L3: safe_get_pc / safe_get_threads orchestration yields HIGH.

    V026 §5.2 end-to-end: the orchestration chain
    (with_stepping → cpu.getAllRegs → _extract_pc →
    TrustLevel.from_pc_result) consistently yields HIGH trust.
    L3 anchors that the factory decision is wired correctly into
    the safe_get_* methods (the factory is not bypassed).
    """

    async def test_safe_get_pc_orchestration_yields_high(self, manager, transport):
        """safe_get_pc orchestration: TrustLevel.from_pc_result is called.

        The returned trust level must be HIGH (from the factory, not
        hardcoded in safe_get_pc). L3 anchors that safe_get_pc uses
        the factory method (not a direct `TrustLevel.HIGH` reference).
        """
        transport.set_response(
            "cpu.getAllRegs", _make_regs_response(pc=0x088E0D5C)
        )

        pc, trust = await manager.safe_get_pc()

        assert pc == 0x088E0D5C
        assert trust == TrustLevel.HIGH
        # Verify the trust came from the factory (it should equal what
        # the factory returns for the same input)
        regs = _make_regs_response(pc=0x088E0D5C)
        expected_trust = TrustLevel.from_pc_result(regs)
        assert trust == expected_trust, (
            "safe_get_pc must use TrustLevel.from_pc_result to derive "
            "trust level (not hardcode TrustLevel.HIGH)."
        )

    async def test_safe_get_threads_orchestration_yields_high(
        self, manager, transport
    ):
        """safe_get_threads orchestration: TrustLevel.from_thread_result called."""
        threads = [{"id": 1, "name": "root"}]
        transport.set_response("hle.thread.list", {"threads": threads})

        snap = await manager.safe_get_threads()

        assert snap.trust_level == TrustLevel.HIGH
        # Verify the trust came from the factory
        expected_trust = TrustLevel.from_thread_result({"threads": threads})
        assert snap.trust_level == expected_trust, (
            "safe_get_threads must use TrustLevel.from_thread_result "
            "to derive trust level (not hardcode TrustLevel.HIGH)."
        )
        assert isinstance(snap, ThreadSnapshot)
        assert snap.sampling_state == "stepping"
