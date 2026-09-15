"""H2 Phase 2 acceptance: SessionManager.start_session(resilient=True).

A2-1: wedge injection → self-heal relaunch with a STABLE session_id;
extra["recovered"]/ppsspp_version folded in; SessionResponse surfaces them.
A2-2: GPU backend blacklist quarantined (rename, never delete); the
PPSSPP_DFX_BOOT_HEAL_QUARANTINE gate disables the file touch.
A2-3: exhaustion → BootTimeout with the attempt count; session entry
discarded so callers observe clean state.
A4 audit: dead-PID port conflicts were ALREADY skipped by
_check_port_conflict — this test locks that regression.
"""

from __future__ import annotations

import asyncio
import functools
import os
from pathlib import Path
from typing import Any

import pytest

from ppsspp_dfx_mcp.errors import BootTimeout, SessionNotFound
from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session import session_manager as sm
from ppsspp_dfx_mcp.views.session import SessionResponse

STUB_PID = os.getpid()  # alive on any test runner (alive_check must pass)


class _WedgeState:
    """Shared mutable state across stub instances (per-test isolated).

    ``fail_launches``: how many LAUNCH attempts wedge (transport reads
    fail). Launch N (1-based) succeeds iff N > fail_launches — the mode
    is derived from the launch counter, so the heal-path assertion is
    deterministic (no flip race between teardown and the next gate).
    """

    def __init__(self, fail_launches: int = 1) -> None:
        self.fail_launches = fail_launches
        self.launchers: list["_WedgeLauncher"] = []

    @property
    def transport_mode(self) -> str:
        if len(self.launchers) > self.fail_launches:
            return "ok"
        return "fail"


class _WedgeLauncher:
    """No-arg-constructible launcher stub (start_session does PpssppLauncher())."""

    def __init__(self, state: _WedgeState, exe_path: Path) -> None:
        self._state = state
        self.exe_path = exe_path
        self.ws_port: int | None = 12345
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self, iso_path: Path) -> Any:
        self.start_calls += 1
        self._state.launchers.append(self)
        return type("_StubProc", (), {"pid": STUB_PID})()

    def stop(self) -> None:
        self.stop_calls += 1


class _StubTransport:
    """In-memory WsTransport stand-in for the bind phase + readiness gate.

    Implements the FULL surface the bind path touches: `.events` (the
    observer dispatcher consumes it — a missing attribute would spin the
    dispatcher's log-and-continue loop at 100% CPU) and
    broadcast.config.set (observer.start's defensive first call).
    """

    def __init__(self, state: _WedgeState) -> None:
        self._state = state
        self.version_info: dict[str, Any] | None = {
            "name": "PPSSPP", "version": "1.19-fake",
        }
        self.closed = False
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def connect(self) -> None:
        pass

    async def send_version(self) -> dict[str, Any]:
        return dict(self.version_info or {})

    async def close(self) -> None:
        self.closed = True

    def is_connected(self) -> bool:
        return not self.closed

    async def call(self, event: str, **params: Any) -> dict[str, Any]:
        if event == "memory.read_u32":
            if self._state.transport_mode == "fail":
                raise RuntimeError("PPSSPP error: CPU not started (level=2)")
            return {"value": 0x27BDFFC0}
        return {}  # broadcast.config.set and other session-level calls


def _patch_production(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: _WedgeState,
) -> tuple[Path, Path]:
    """Production-mode plumbing: real-looking ISO + exe tree, stub
    launcher/transport, instant probe polling, no WS in sight."""
    monkeypatch.setattr(sm, "test_mode", lambda: "")
    monkeypatch.setattr(sm, "wedge_cooldown", lambda: None)
    monkeypatch.setattr(
        sm, "probe_cpu_ready",
        functools.partial(sm.probe_cpu_ready, poll_interval_s=0.02),
    )
    monkeypatch.setattr(sm, "_probe_ws_connection", _async_true)

    import ppsspp_dfx_mcp.core.transport as transport_mod
    monkeypatch.setattr(
        transport_mod, "WsTransport", lambda *a, **k: _StubTransport(state)
    )

    iso = tmp_path / "game.iso"
    iso.write_bytes(b"ISO")
    exe_dir = tmp_path / "ppsspp"
    blacklist = exe_dir / "memstick" / "PSP" / "SYSTEM" / (
        "FailedGraphicsBackends.txt"
    )
    blacklist.parent.mkdir(parents=True, exist_ok=True)
    blacklist.write_text("DIRECT3D11,VULKAN", encoding="utf-8")

    def _make_launcher() -> _WedgeLauncher:
        return _WedgeLauncher(state, exe_dir / "ppsspp.exe")

    monkeypatch.setattr(sm, "PpssppLauncher", _make_launcher)
    return iso, blacklist


async def _async_true(ws_url: str) -> bool:
    return True


@pytest.mark.asyncio
async def test_resilient_heals_one_wedge_with_stable_session_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    isolated_sessions_path: Path,
) -> None:
    """A2-1 + A2-2: one wedged launch → heal (kill → quarantine) →
    relaunch succeeds with the SAME session_id, recovered=1, version
    fingerprint folded in, blacklist renamed (never deleted)."""
    state = _WedgeState(fail_launches=1)  # launch 1 wedges, launch 2 ok
    iso, blacklist = _patch_production(monkeypatch, tmp_path, state)

    sess = await sm.get_session_manager().start_session(
        str(iso), resilient=True, ready_timeout_s=0.4, max_restarts=2,
    )

    assert len(state.launchers) == 2  # one relaunch
    assert state.launchers[0].stop_calls == 1  # wedged attempt torn down
    assert all(s.session_id == sess.session_id for s in
               await sm.get_session_manager().list_sessions())
    assert sess.extra["recovered"] == 1
    assert sess.extra["ppsspp_version"] == {
        "name": "PPSSPP", "version": "1.19-fake",
    }
    # Quarantine: original gone, evidence preserved as .bak-*.
    assert not blacklist.exists()
    baks = list(blacklist.parent.glob("FailedGraphicsBackends.txt.bak-*"))
    assert len(baks) == 1
    assert baks[0].read_text(encoding="utf-8") == "DIRECT3D11,VULKAN"
    # View surfaces recovered for agents.
    view = SessionResponse.from_session(sess)
    assert view.recovered == 1
    assert view.ppsspp_version == {"name": "PPSSPP", "version": "1.19-fake"}


@pytest.mark.asyncio
async def test_resilient_exhaustion_raises_boot_timeout_and_discards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    isolated_sessions_path: Path,
) -> None:
    """A2-3: every attempt wedges → BootTimeout with the attempt count and
    quarantine report; the dead session entry is discarded."""
    state = _WedgeState(fail_launches=99)  # every attempt wedges
    iso, blacklist = _patch_production(monkeypatch, tmp_path, state)

    with pytest.raises(BootTimeout) as ei:
        await sm.get_session_manager().start_session(
            str(iso), resilient=True, ready_timeout_s=0.3, max_restarts=2,
        )
    msg = str(ei.value)
    assert "3 launch attempt(s)" in msg
    assert "quarantined" in msg
    assert len(state.launchers) == 3
    assert all(l.stop_calls == 1 for l in state.launchers)
    assert not blacklist.exists()  # quarantined on every heal
    with pytest.raises(SessionNotFound):
        await sm.get_session_manager().get_session_state("nonexistent-probe")
    # The exhausted session id must NOT linger in sessions.json.
    entries = await sm.get_session_manager().list_sessions()
    assert all(s.extra.get("recovered") is None for s in entries)


@pytest.mark.asyncio
async def test_resilient_disabled_by_default_keeps_legacy_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    isolated_sessions_path: Path,
) -> None:
    """裁决②: default OFF — a wedged CPU with resilient=False returns the
    session immediately (historical best-effort), touches no files."""
    state = _WedgeState(fail_launches=99)
    iso, blacklist = _patch_production(monkeypatch, tmp_path, state)

    sess = await sm.get_session_manager().start_session(str(iso))
    assert sess.extra.get("recovered") is None
    # Phase 0: the version fingerprint folds in for EVERY bound session.
    assert sess.extra["ppsspp_version"] == {"name": "PPSSPP", "version": "1.19-fake"}
    assert len(state.launchers) == 1
    assert state.launchers[0].stop_calls == 0
    assert blacklist.exists()  # untouched


@pytest.mark.asyncio
async def test_quarantine_env_gate_disables_file_touch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    isolated_sessions_path: Path,
) -> None:
    """A2-2 (gate): PPSSPP_DFX_BOOT_HEAL_QUARANTINE=0 → heal still
    relaunches, but the blacklist file is left alone."""
    state = _WedgeState(fail_launches=1)
    iso, blacklist = _patch_production(monkeypatch, tmp_path, state)
    monkeypatch.setenv("PPSSPP_DFX_BOOT_HEAL_QUARANTINE", "0")

    sess = await sm.get_session_manager().start_session(
        str(iso), resilient=True, ready_timeout_s=0.3, max_restarts=1,
    )

    assert sess.extra["recovered"] == 1
    assert blacklist.exists()  # gate kept the file in place
    assert not list(blacklist.parent.glob("*.bak-*"))


@pytest.mark.asyncio
async def test_handshake_hang_wedge_variant_heals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    isolated_sessions_path: Path,
) -> None:
    """Device-creation-hang variant: the port listens but the debugger
    never accepts → transport never binds → gate wedges → heal."""
    state = _WedgeState(fail_launches=0)  # transport reads would succeed
    iso, blacklist = _patch_production(monkeypatch, tmp_path, state)

    async def _ws_hang(ws_url: str) -> bool:
        # Bind only happens from launch 2 on: launch 1 wedges at the
        # handshake (device-creation-hang variant).
        return len(state.launchers) >= 2

    monkeypatch.setattr(sm, "_probe_ws_connection", _ws_hang)

    sess = await sm.get_session_manager().start_session(
        str(iso), resilient=True, ready_timeout_s=0.3, max_restarts=1,
    )
    assert len(state.launchers) == 2
    assert sess.extra["recovered"] == 1


@pytest.mark.asyncio
async def test_dead_pid_port_conflict_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    isolated_sessions_path: Path,
) -> None:
    """A4 audit lock: a conflicting session whose PID is dead does NOT
    block a new start (pre-existing _check_port_conflict behavior —
    this test keeps it from regressing)."""
    state = _WedgeState(fail_launches=0)
    iso, _blacklist = _patch_production(monkeypatch, tmp_path, state)

    stale = Session(
        session_id="stale-dead",
        iso_path=str(iso),
        pid=999999,  # dead on any reasonable runner
        ws_url="ws://127.0.0.1:12345/debugger",
    )
    isolated_sessions_path.parent.mkdir(parents=True, exist_ok=True)
    import json
    isolated_sessions_path.write_text(json.dumps(
        {"stale-dead": sm._session_to_dict(stale)}), encoding="utf-8")

    sess = await sm.get_session_manager().start_session(str(iso))
    assert sess.ws_url.endswith(":12345/debugger")
