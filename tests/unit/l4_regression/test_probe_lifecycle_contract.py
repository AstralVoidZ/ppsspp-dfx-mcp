"""Probe lifecycle: session ownership, clear protection, empty-registry hint
(US2 / contracts C2.1-C2.4).

D17 (measured 2026-09-30 on the real server, 2 mirror images):
  1. `clear` emptied the registry and the one-shot `_SEEDED` latch meant
     the 4 built-ins never came back -- for the whole process lifetime.
  2. The registry was process-global, so a probe registered in one session
     was visible from every other session.

Together these silently broke `state_observer`, `batch_step(state_probe)`,
`frame_snapshot(probes=)` and `breakpoint(action='stats')` at once.

Each test names the failure it would catch.
"""

from __future__ import annotations

import pathlib
from pathlib import Path

import pytest
import yaml

from ppsspp_dfx_mcp.tools import state_observer as so

BUILTIN_NAMES = {"game_mode", "cursor_x", "cursor_y", "prim_counter"}

SEED_YAML = {
    "state_probes": {
        "game_mode": {"address": 0x08A0D000, "size": 4, "description": "mode"},
        "cursor_x": {"address": 0x09087B44, "size": 2, "description": "x"},
        "cursor_y": {"address": 0x09087B46, "size": 2, "description": "y"},
        "prim_counter": {"address": 0x09087B3C, "size": 4, "description": "prims"},
    }
}

SESSION_A = "session-aaaa"
SESSION_B = "session-bbbb"


@pytest.fixture
def seeded_config(tmp_path, monkeypatch) -> Path:
    """Point config at a temp dir holding a real state_probes block.

    Returns the config dir so a test can rewrite the YAML (e.g. to model a
    deployment with no `state_probes` section).
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "addresses.yaml").write_text(yaml.safe_dump(SEED_YAML), encoding="utf-8")
    monkeypatch.setenv("PPSSPP_DFX_CONFIG_DIR", str(cfg))
    return cfg


def _registry_for(session_id: str) -> dict:
    """The registry the implementation associates with `session_id`."""
    by_session = getattr(so, "_REGISTRY_BY_SESSION", None)
    if isinstance(by_session, dict):
        return by_session.setdefault(session_id, {})
    # Pre-fix shape: one process-wide dict.
    return so._REGISTRY


class TestSessionOwnership:
    """C2.1 - probes belong to a session."""

    @pytest.mark.asyncio
    async def test_probe_registered_in_a_is_invisible_to_b(
        self, probe_registry_reset, seeded_config, client, transport
    ) -> None:
        """The cross-session leak that made results untrustworthy.

        Fails if a probe registered under SESSION_A shows up when SESSION_B
        lists its probes.
        """
        reg_a = _registry_for(SESSION_A)
        reg_b = _registry_for(SESSION_B)
        reg_a["only_in_a"] = so.StateProbe(name="only_in_a", address=0x1000, size=4, description="")
        assert "only_in_a" in reg_a
        assert "only_in_a" not in reg_b, (
            "probe leaked across sessions -- an agent would read another "
            "session's memory layout as if it were its own"
        )

    @pytest.mark.asyncio
    async def test_each_session_seeds_its_own_builtins(
        self, probe_registry_reset, seeded_config
    ) -> None:
        """Two live sessions must both be able to observe.

        Fails if seeding is keyed globally such that the second session
        ends up empty.
        """
        _registry_for(SESSION_A)
        _registry_for(SESSION_B)
        so._seed_from_yaml(SESSION_A)
        so._seed_from_yaml(SESSION_B)
        for sid in (SESSION_A, SESSION_B):
            got = set(_registry_for(sid))
            assert got >= BUILTIN_NAMES, f"{sid} missing built-ins: {BUILTIN_NAMES - got}"


class TestClearProtection:
    """C2.2 - clear must not remove built-ins."""

    @pytest.mark.asyncio
    async def test_builtin_probes_survive_clear(self, probe_registry_reset, seeded_config) -> None:
        """The D17 core: after clear, the 4 built-ins must still be listed.

        Fails if clear empties the registry, or if the built-ins are not
        re-seeded afterwards.
        """
        so._seed_from_yaml(SESSION_A)
        assert set(_registry_for(SESSION_A)) >= BUILTIN_NAMES

        so._clear_probes(SESSION_A, name=None)

        got = set(_registry_for(SESSION_A))
        assert got >= BUILTIN_NAMES, (
            f"clear removed built-ins: {BUILTIN_NAMES - got}; the probe "
            f"library is now permanently degraded for this session"
        )

    @pytest.mark.asyncio
    async def test_clear_removes_user_probes(self, probe_registry_reset, seeded_config) -> None:
        """Protection must not make clear a no-op."""
        reg = _registry_for(SESSION_A)
        so._seed_from_yaml(SESSION_A)
        reg["scratch"] = so.StateProbe(name="scratch", address=0x2000, size=4, description="mine")
        so._clear_probes(SESSION_A, name=None)
        assert "scratch" not in reg, "clear must still remove user probes"

    @pytest.mark.asyncio
    async def test_clear_one_builtin_leaves_others(
        self, probe_registry_reset, seeded_config
    ) -> None:
        """Targeted clear on a built-in name must be a no-op, not a removal."""
        so._seed_from_yaml(SESSION_A)
        so._clear_probes(SESSION_A, name="game_mode")
        assert set(_registry_for(SESSION_A)) >= BUILTIN_NAMES, (
            "clearing one built-in by name must not delete it"
        )

    @pytest.mark.asyncio
    async def test_clearing_unknown_name_is_idempotent(
        self, probe_registry_reset, seeded_config
    ) -> None:
        """clear must stay the idempotent action its docstring promises."""
        so._seed_from_yaml(SESSION_A)
        so._clear_probes(SESSION_A, name="never_registered")
        so._clear_probes(SESSION_A, name="never_registered")
        assert set(_registry_for(SESSION_A)) >= BUILTIN_NAMES


class TestReseedIdempotence:
    """C2.4 - re-seeding keeps user entries and repeats cleanly."""

    @pytest.mark.asyncio
    async def test_reseed_keeps_user_probes(self, probe_registry_reset, seeded_config) -> None:
        so._seed_from_yaml(SESSION_A)
        _registry_for(SESSION_A)["keepme"] = so.StateProbe(
            name="keepme", address=0x3000, size=4, description=""
        )
        so._seed_from_yaml(SESSION_A)  # second call: must be a no-op
        reg = _registry_for(SESSION_A)
        assert "keepme" in reg, "re-seed dropped a user probe"
        assert set(reg) >= BUILTIN_NAMES

    @pytest.mark.asyncio
    async def test_reseed_is_idempotent(self, probe_registry_reset, seeded_config) -> None:
        so._seed_from_yaml(SESSION_A)
        first = dict(_registry_for(SESSION_A))
        so._seed_from_yaml(SESSION_A)
        assert dict(_registry_for(SESSION_A)) == first, (
            "second seed changed the registry; seeding is not idempotent"
        )


class TestEmptyRegistryIsSelfDescribing:
    """C2.3 - an empty registry must not look like a typo."""

    @pytest.mark.asyncio
    async def test_observe_on_empty_registry_explains_how_to_recover(
        self, probe_registry_reset, seeded_config, client, transport
    ) -> None:
        """A bare 'unknown probe; registered: []' sends the caller hunting
        for a typo instead of telling them the library is empty."""
        from contextlib import asynccontextmanager
        from unittest.mock import patch

        # A config WITH state_probes can never look empty: the tool seeds
        # before resolving, so clear is not a route to an empty library.
        # The reachable case is a config with no `state_probes` section --
        # that is what this models.
        (tmp_cfg := pathlib.Path(seeded_config) / "addresses.yaml")
        tmp_cfg.write_text(yaml.safe_dump({"known_modules": {}}), encoding="utf-8")
        # T053 S-4：pop registry + discard latch == 生产语义化回收 API
        # drop_session（no-op 容忍未知会话），不再直写生产私有容器。
        so.drop_session(SESSION_A)

        @asynccontextmanager
        async def _cm(_sid):
            raise AssertionError("must not open a connection to report a name error")
            yield  # pragma: no cover

        with (
            patch("ppsspp_dfx_mcp.tools.state_observer.session_client", _cm),
            pytest.raises(Exception) as exc,
        ):
            await so.state_observer(session_id=SESSION_A, action="observe", names="game_mode")
        msg = str(exc.value)
        assert "game_mode" in msg, msg
        assert any(hint in msg.lower() for hint in ("register", "empty", "seed", "no probes")), (
            f"error must say how to recover, got: {msg}"
        )
