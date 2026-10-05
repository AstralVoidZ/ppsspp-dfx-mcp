"""Enum + numeric parameter validation (US1 / contract C1.1, C1.2).

D12/D15 measured 2026-09-30 on the real MCP server:
- `ppsspp_disassemble(count=0)` silently returned 10 instructions;
  `count=-5` returned an empty list while echoing `count=0`.
- `device_walkthrough(phase='nope')` silently ran the `advance` branch and
  returned `elapsed_s=988548.7` (~11 days).

Some silent coercions in this codebase are deliberate and documented
(`count=0 -> 10` is the "M2" fallback). These tests therefore pin the
contract, not the absence of coercion: whatever the tool does, the caller
must be able to TELL. A test fails if a substitution becomes invisible.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ppsspp_dfx_mcp.errors import ArgsInvalid


def _find_repo_root() -> Path | None:
    """Walk up from this file until the recipe's repo tree is visible.

    Hard-coding a parents[N] index is brittle: the package sits at
    <repo>/mcps/ppsspp-dfx-mcp, so the depth depends on where pytest was
    invoked from. Looking for the file itself cannot be wrong.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "tools" / "topx_diagnostics" / "recipe" / "device_walkthrough.py"
        if candidate.exists():
            return parent
    return None


def _load_recipe():
    """Import device_walkthrough from the repo tree, or skip."""
    root = _find_repo_root()
    if root is None:
        pytest.skip("device_walkthrough.py not found (package-only checkout)")
    recipe = root / "tools" / "topx_diagnostics" / "recipe" / "device_walkthrough.py"
    parent = str(recipe.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    try:
        import importlib

        return importlib.import_module(recipe.stem)
    finally:
        if parent in sys.path:
            sys.path.remove(parent)


class TestEnumValidation:
    """C1.1 - illegal enum values are rejected and all options are listed."""

    def test_walkthrough_phase_rejects_illegal_value(self) -> None:
        """D15: phase is documented as load|wait|advance; anything else fails.

        Fails if an illegal phase is silently mapped onto a real branch.
        """
        mod = _load_recipe()
        with pytest.raises(ValueError) as exc:
            mod.DeviceWalkthroughInput(phase="nope")
        msg = str(exc.value)
        assert "load" in msg and "wait" in msg and "advance" in msg, (
            f"rejection must list the legal phases, got: {msg}"
        )

    @pytest.mark.parametrize("bad", ["nope", "", "LOAD", "load "])
    def test_walkthrough_phase_rejects_near_misses(self, bad: str) -> None:
        """Case and whitespace variants must not pass as 'close enough'."""
        mod = _load_recipe()
        with pytest.raises(ValueError):
            mod.DeviceWalkthroughInput(phase=bad)

    def test_walkthrough_phase_accepts_documented_values(self) -> None:
        """The legal set must stay legal, or C1.1 is over-tightened."""
        mod = _load_recipe()
        for good in ("load", "wait", "advance"):
            assert mod.DeviceWalkthroughInput(phase=good).phase == good


class TestNumericBounds:
    """C1.2 - numeric parameters must not be silently replaced."""

    @pytest.mark.asyncio
    async def test_disasm_negative_count_is_rejected(self) -> None:
        """D12: count=-5 returned an empty list while echoing count=0.

        Must be rejected before any session lookup, so the caller gets the
        real cause instead of a confusing SESSION_NOT_FOUND.
        """
        from ppsspp_dfx_mcp.tools.memory import disassemble

        with pytest.raises(ArgsInvalid) as exc:
            await disassemble(address="0x08804000", count=-5, session_id="s")
        assert "count" in str(exc.value)

    @pytest.mark.asyncio
    async def test_disasm_rejects_before_session_lookup(self) -> None:
        """Validation order matters: argument errors must not look like
        session errors, or the caller debugs the wrong thing entirely."""
        from ppsspp_dfx_mcp.errors import SessionNotFound
        from ppsspp_dfx_mcp.tools.memory import disassemble

        with pytest.raises(ArgsInvalid):
            await disassemble(address="0x08804000", count=-1, session_id="nope")
        # A VALID count with a bad session must still be a session error.
        with pytest.raises(SessionNotFound):
            await disassemble(address="0x08804000", count=4, session_id="nope")

    @pytest.mark.asyncio
    async def test_disasm_zero_count_is_explicit(self, client, transport) -> None:
        """count=0 is a documented fallback to 10, but must SAY so.

        Fails if the response is indistinguishable from an ordinary
        10-instruction result, leaving the substitution invisible.
        """
        from ppsspp_dfx_mcp.tools.memory import disassemble

        transport.set_response("memory.disasm", {"lines": [{"text": f"op{i}"} for i in range(10)]})
        with _stub_session(client):
            resp = await disassemble(address="0x08804000", count=0, session_id="s")
        assert "note" in resp, f"count=0 substitution hidden; keys={sorted(resp)}"
        assert "10" in resp["note"]

    @pytest.mark.asyncio
    async def test_disasm_over_cap_is_reported(self, client, transport) -> None:
        """D12: an over-cap count was silently truncated to the cap.

        Fails if a caller asking for max+1 gets back max with no signal.
        """
        from ppsspp_dfx_mcp.tools.memory import _MAX_DISASM_COUNT, disassemble

        transport.set_response(
            "memory.disasm",
            {"lines": [{"text": "op"} for _ in range(_MAX_DISASM_COUNT)]},
        )
        with _stub_session(client):
            resp = await disassemble(
                address="0x08804000", count=_MAX_DISASM_COUNT + 1, session_id="s"
            )
        assert "note" in resp, f"truncation hidden; keys={sorted(resp)}"
        assert str(_MAX_DISASM_COUNT) in resp["note"]


@contextlib.contextmanager
def _stub_session(client):
    """Point tools.memory.session_client at an already-built fake client.

    The L1 `client` fixture is a PpssppDebugClient over FakeTransport, so the
    tool body runs end to end without a real PPSSPP.
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm(_session_id):
        yield client

    with patch("ppsspp_dfx_mcp.tools.memory.session_client", _cm):
        yield
