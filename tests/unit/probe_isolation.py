"""Probe-registry reset helper for tests (T005).

Kept as a plain importable module rather than a `conftest.py` so the reset
logic is unit-testable in its own right, and so the fixtures can be
registered from a single explicit place (see tests/unit/conftest.py).

D17 (measured 2026-09-30): `tools/state_observer.py` keeps the probe
registry in a MODULE-LEVEL dict guarded by a one-shot `_SEEDED` latch.
Two measured consequences:

  1. `clear` empties the registry, and because `_SEEDED` is already True
     it never re-seeds -- the 4 built-in probes stay gone for the whole
     server-process lifetime.
  2. The registry is process-wide, so a probe registered in one session
     is visible from every other session.

(2) makes test order matter: without an explicit reset, a test that
registers a probe leaks it into every later test and a test that clears
leaks an EMPTY registry into every later test. Both look like defects in
the code under test.

After the US2 fix the registry becomes session-keyed and this module
keeps working unchanged: it clears whatever the current storage is, so
tests written against contract C2.1 stay valid before and after.
"""

from __future__ import annotations

from typing import Any

from ppsspp_dfx_mcp.tools import state_observer as so


def _reset() -> None:
    """Drop every registered probe and re-arm the seeding latch.

    Tolerant of both shapes:
      * pre-fix  -- module-level ``_REGISTRY`` + ``_SEEDED`` bool
      * post-fix -- per-session mapping + seeded-id set
    """
    reg = getattr(so, "_REGISTRY", None)
    if isinstance(reg, dict):
        reg.clear()

    seeded = getattr(so, "_SEEDED", None)
    if isinstance(seeded, bool):
        so._SEEDED = False

    for attr in ("_REGISTRY_BY_SESSION", "_REGISTRIES"):
        value = getattr(so, attr, None)
        if isinstance(value, dict):
            value.clear()

    for attr in ("_SEEDED_BY_SESSION", "_SEEDED_SESSIONS"):
        value = getattr(so, attr, None)
        if isinstance(value, (set, dict)):
            value.clear()


def reset_probe_registry() -> None:
    """Public entry point; kept separate so tests read clearly."""
    _reset()


def register_fixtures(namespace: dict[str, Any]) -> None:
    """Register the reset fixtures into a conftest namespace."""
    register_probe_fixtures(namespace)


def register_probe_fixtures(namespace: dict[str, Any]) -> None:
    """Register the probe-registry reset fixtures into a conftest namespace.

    Called from each tests/unit subdirectory conftest.py so the fixtures are
    discoverable (pytest only auto-loads files named conftest.py, and this
    package supplies fixtures per subdirectory) while the reset logic stays
    in an importable, independently testable module.
    """
    import pytest

    @pytest.fixture
    def probe_registry_reset() -> Any:
        """Reset the probe registry before AND after the test.

        The post-test reset matters as much as the pre-test one: without it
        a test that registers a probe pollutes every test that runs after.
        """
        _reset()
        try:
            yield so
        finally:
            _reset()

    @pytest.fixture
    def clean_probe_registry(probe_registry_reset: Any) -> Any:
        """Alias for call sites that only need isolation, no handle."""
        return probe_registry_reset

    namespace["probe_registry_reset"] = probe_registry_reset
    namespace["clean_probe_registry"] = clean_probe_registry
