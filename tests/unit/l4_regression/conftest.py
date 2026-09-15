"""L4 regression test fixtures.

L4 tests lock in fixes for contract violations (V006/V007/V008/V009
etc.): each test names the violation it guards against.

Reuses the canonical FakeTransport from `tests/fake_transport/`
(injected to sys.path by root conftest.py). That FakeTransport records every
`call()` invocation in `transport.calls` as `(event, params)` tuples and
returns configurable responses via `set_response(event, ...)`.

Stepping state-transition faf handlers are pre-configured so L4 tests
that exercise with_stepping-wrapped methods (set_reg / evaluate /
backtrace / func_scan / func_add / func_remove operate without per-test state setup. Tests that need
to bypass the wrap (e.g. to isolate the underlying call) can override
with `transport.set_state({"stepping": True})`.
"""

from __future__ import annotations

from typing import Any

import pytest

from fake_transport import FakeTransport
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


def _set_stepping_true(t: FakeTransport, **params: Any) -> None:
    cur = t.state
    t.set_state({**cur, "stepping": True})


def _set_stepping_false(t: FakeTransport, **params: Any) -> None:
    cur = t.state
    t.set_state({**cur, "stepping": False})


@pytest.fixture
def transport() -> FakeTransport:
    """Fresh FakeTransport with stepping faf handlers wired.

    Mirrors the L1/L3 fixture pattern: fire_and_forget("cpu.stepping")
    sets stepping=True (pause), fire_and_forget("cpu.resume") sets
    stepping=False (resume). Without these handlers, with_stepping's
    wait_for_state would time out.
    """
    t = FakeTransport()
    t.set_state({"stepping": False})
    t.set_faf_handler("cpu.stepping", _set_stepping_true)
    t.set_faf_handler("cpu.resume", _set_stepping_false)
    return t


@pytest.fixture
def client(transport: FakeTransport) -> PpssppDebugClient:
    """PpssppDebugClient backed by FakeTransport.

    PpssppDebugClient.__init__ builds a SteppingManager from the
    transport. FakeTransport satisfies the duck-typed `_TransportLike`
    protocol (it has `call`/`fire_and_forget`/`wait_for_state`), so the
    constructor accepts it.
    """
    return PpssppDebugClient(transport)
