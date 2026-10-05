"""Probe registry isolation fixture self-test (T005).

Kept as a canary for the reset helper in `probe_isolation`, which every
other probe test depends on. The behaviour those tests pin lives in
`test_probe_lifecycle_contract.py`; this file only proves the fixture
actually isolates, because a silently broken fixture would make every
other probe test pass for the wrong reason.

Post-D17 the registry is keyed by session id, so isolation means
per-session, and the reset helper must clear every session.
"""

from __future__ import annotations

import pytest
from _support import state as state_seam  # T053 S-4：集中式测试支撑缝
from probe_isolation import _reset

from ppsspp_dfx_mcp.tools import state_observer as so

SESSION_A = "iso-a"
SESSION_B = "iso-b"


def _probe(name: str, address: int) -> so.StateProbe:
    return so.StateProbe(name=name, address=address, size=4, description="")


class TestFixtureIsolation:
    def test_fixture_clears_every_session(self, probe_registry_reset) -> None:
        """Canary: the fixture must leave NO session holding state.

        Fails if the reset helper still targets the removed module-level
        dict, or if it misses the per-session mapping — either way every
        later probe test could inherit another test's probes.
        """
        so._registry(SESSION_A)["leak_a"] = _probe("leak_a", 0x1000)
        so._registry(SESSION_B)["leak_b"] = _probe("leak_b", 0x2000)

        _reset()

        assert "leak_a" not in so._registry(SESSION_A), "fixture left session A dirty"
        assert "leak_b" not in so._registry(SESSION_B), "fixture left session B dirty"

    def test_reset_clears_seeded_latch(self, probe_registry_reset) -> None:
        """Re-seeding must be re-armed by the reset, else a later test that
        seeds would be silently skipped and assert against stale data."""
        # T053 S-4：种子经支撑缝写入，不再直写 _SEEDED_BY_SESSION。
        state_seam.seed_probe_seeded(SESSION_A)
        _reset()
        assert SESSION_A not in so._SEEDED_BY_SESSION, (
            "reset must re-arm seeding so the next seed is not a no-op"
        )

    @pytest.mark.asyncio
    async def test_sessions_do_not_share_probes(self, probe_registry_reset) -> None:
        """The D17 core: one session's probe must not be visible in another."""
        so._registry(SESSION_A)["only_a"] = _probe("only_a", 0x1000)
        assert "only_a" not in so._registry(SESSION_B)
