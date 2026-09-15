"""L1 contract test fixtures.

L1 tests assert pure forwarding behavior: each DebugClient method
forwards the correct PPSSPP WebSocket event name + parameters and
extracts the correct return field. Anchors: PPSSPP C++ source line
numbers + contract table (NOT violation numbers — that's L4).

Reuses the canonical FakeTransport from `tests/fake_transport/`
(injected to sys.path by root conftest.py).

FakeTransport is pre-loaded with real PPSSPP fixtures captured by
`record_fixtures.py` (Phase 4 upgrade). Tests that assert a specific
response value can still call `transport.set_response(event, {...})`
to override the loaded default — FakeTransport's `set_response` is
replace semantics, so the explicit override wins.

Stepping state-transition faf handlers (cpu.stepping / cpu.resume) are
wired after fixture loading so DebugClient methods that internally call
`with_stepping` (set_reg / evaluate / backtrace / func_scan / func_add /
func_remove) operate without manual state
setup per test. This mirrors the L3 orchestration fixture pattern.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from contract_recorder.fixture_loader import load_all
from fake_transport import FakeTransport
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

log = logging.getLogger(__name__)

# Path to recorded real PPSSPP fixtures (one JSON file per event).
# Produced by `python -m ppsspp_dfx_mcp.scripts.record_fixtures`.
# conftest.py is at tests/unit/l1_contract/conftest.py → parents[2]=tests.
_REAL_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "cassettes" / "fixtures"
)


def _set_stepping_true(t: FakeTransport, **params: Any) -> None:
    cur = t.state
    t.set_state({**cur, "stepping": True})


def _set_stepping_false(t: FakeTransport, **params: Any) -> None:
    cur = t.state
    t.set_state({**cur, "stepping": False})


@pytest.fixture
def transport() -> FakeTransport:
    """FakeTransport pre-loaded with real PPSSPP fixtures + stepping faf handlers.

    Loads recorded real PPSSPP responses from
    `tests/cassettes/fixtures/` into the FakeTransport via `load_all`.
    After loading, stepping faf handlers are wired so:

    - Initial state: stepping=False (running)
    - fire_and_forget("cpu.stepping") → state stepping=True (pause)
    - fire_and_forget("cpu.resume") → state stepping=False (resume)

    Tests that need a specific response value can still call
    `transport.set_response(event, {...})` — the explicit override
    replaces the loaded default (FakeTransport.set_response is
    replace-semantics, see fake_transport.py:L78-90).

    If the fixture directory is missing (fixtures not yet recorded),
    a warning is logged and the FakeTransport is returned with empty
    responses — `call()` returns `{}` for unconfigured events. This
    keeps the L1 suite runnable before recording has been done.
    """
    t = FakeTransport()
    t.set_state({"stepping": False})
    if _REAL_FIXTURE_DIR.is_dir():
        load_all(_REAL_FIXTURE_DIR, t)
    else:
        log.warning(
            "real PPSSPP fixtures not found at %s — run "
            "`python -m ppsspp_dfx_mcp.scripts.record_fixtures` "
            "to enable real-response defaults",
            _REAL_FIXTURE_DIR,
        )
    # Always wire stepping faf handlers after load_all (load_all does
    # not set faf handlers — fire_and_forget fixtures are not injected
    # via set_response; see fixture_loader.py:L65-67).
    t.set_faf_handler("cpu.stepping", _set_stepping_true)
    t.set_faf_handler("cpu.resume", _set_stepping_false)
    return t


@pytest.fixture
def client(transport: FakeTransport) -> PpssppDebugClient:
    """PpssppDebugClient backed by FakeTransport."""
    return PpssppDebugClient(transport)
