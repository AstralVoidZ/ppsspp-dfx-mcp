"""Integration test fixtures.

Integration tests exercise end-to-end flows that cross module boundaries:
- session tool (tools/session.py) → session_manager → Session model
- server._lifespan → session_manager + manifest loader
- ScriptManifest → config_dir → YAML file

Anchor: usage scenarios (NOT violation numbers — that's L4; NOT pure
forwarding — that's L1). These tests verify the wiring between modules
is correct for representative user flows.

Environment note: same PyWin32 stub injection as l2_mcp_contract/conftest.py
— needed for tests that import `ppsspp_dfx_mcp.server` (lifespan tests).
Session-lifecycle and script-manifest tests do NOT import server, so
the stubs are harmless overhead for them.

Phase 6 additions — real-PPSSPP end-to-end fixtures:
- `ppsspp_exe_path` / `iso_path` / `cassette_path` — skip-if-missing
  resource probes (CI-safe; tests skip when PPSSPP / ISO / cassette
  are unavailable instead of failing).
- `real_ppsspp_launcher` — session-scoped real PPSSPP process managed
  by PpssppLauncher (random port + appendconfig + runtime port discovery).
  Reused across all tests that need a live PPSSPP to avoid the ~5s
  startup cost per test.
- `real_ppsspp_ws_url` — convenience accessor for the launcher's ws:// URL
  (host + discovered port). Tests use this to construct WsTransport.
- `real_transport` — function-scoped connected WsTransport to the live
  PPSSPP; closed on teardown. Each test gets a fresh transport (no state
  leakage between tests), but the PPSSPP process itself is shared.
- `real_session` — function-scoped session_id via session_manager
  (production path). Used by tests that need the full session lifecycle
  (start_session + stop_session round-trip). Each test launches its own
  PPSSPP subprocess via session_manager; teardown stops it.

Design note: the `real_ppsspp_launcher` fixture is session-scoped to
avoid the ~5s PPSSPP startup cost per test. Tests that need the full
session_manager path (start_session → stop_session) use `real_session`
which is function-scoped and launches its own PPSSPP (the session-scoped
launcher is bypassed for those tests). This split lets most tests reuse
the shared PPSSPP, while session-lifecycle tests exercise the production
code path verbatim.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

# ── Phase 6: real-PPSSPP resource probes (skip-if-missing) ────────────────
#
# All three fixtures skip when the resource is missing rather than failing.
# This lets the test suite run in CI (no PPSSPP / ISO / cassette) without
# modifications, while still exercising real-PPSSPP paths locally.
#
# Paths are resolved relative to this conftest. conftest.py is at
# mcps/ppsspp-dfx-mcp/tests/integration/conftest.py.

_HERE = Path(__file__).resolve().parent
_TESTS_ROOT = _HERE.parent  # tests/
_SRC_ROOT = _HERE.parents[1] / "src"  # ppsspp-dfx-mcp/src/
_CASSETTE_DIR = _TESTS_ROOT / "cassettes"
_CASSETTE_PATH = _CASSETTE_DIR / "real_ppsspp.jsonl"

# Real-mode resources have no in-repo defaults: PPSSPP and a game ISO are
# user-local, so both come from env vars only. Missing env / file skips the
# session (the suite stays green without any external resource).
# Using resolve() so downstream consumers see an absolute path regardless
# of the test's CWD.
_DEFAULT_ISO_PATH = None
_DEFAULT_PPSSPP_EXE = None


def _workspace_root() -> Path:
    """Root holding optional project assets (.ppsspp-dfx/config/).

    Resolution order: PPSSPP_DFX_WORKSPACE_ROOT env override, then the
    nearest ancestor that actually has a .ppsspp-dfx/config/ directory,
    then the package root (standalone layout — config simply absent).
    """
    env = os.environ.get("PPSSPP_DFX_WORKSPACE_ROOT", "")
    if env:
        return Path(env).resolve()
    for cand in (*reversed(_HERE.parents), _HERE):
        if (cand / ".ppsspp-dfx" / "config").is_dir():
            return cand
    return _HERE.parents[1]


@pytest.fixture(scope="session")
def ppsspp_exe_path() -> Path:
    """Return PPSSPP executable path (skip if not found).

    Priority: PPSSPP_DFX_TEST_EXE_PATH env var. No in-repo default —
    a PPSSPP build is a user-local resource.
    Skips the test session if the executable is missing — running
    real-PPSSPP tests without a PPSSPP binary is meaningless.
    """
    raw = os.environ.get("PPSSPP_DFX_TEST_EXE_PATH", "")
    p = Path(raw).expanduser().resolve() if raw else _DEFAULT_PPSSPP_EXE
    if p is None or not p.is_file():
        pytest.skip(
            "PPSSPP executable not configured (set PPSSPP_DFX_TEST_EXE_PATH to your PPSSPP binary)",
            allow_module_level=True,
        )
    return p


@pytest.fixture(scope="session")
def iso_path() -> Path:
    """Return ISO file path (skip if not found).

    Priority: PPSSPP_DFX_TEST_ISO_PATH env var. No in-repo default —
    a game ISO is a user-local resource (do not commit one).
    """
    raw = os.environ.get("PPSSPP_DFX_TEST_ISO_PATH", "")
    p = Path(raw).expanduser().resolve() if raw else _DEFAULT_ISO_PATH
    if p is None or not p.is_file():
        pytest.skip(
            "Game ISO not configured (set PPSSPP_DFX_TEST_ISO_PATH to your ISO path)",
            allow_module_level=True,
        )
    return p


@pytest.fixture(scope="session")
def cassette_path() -> Path:
    """Return the real-PPSSPP cassette path (skip if not found).

    The cassette is the JSONL recording of a real PPSSPP WebSocket
    session (produced by `python -m ppsspp_dfx_mcp.scripts.record_fixtures`).
    Used by ReplayTransport-based tests to validate record→replay roundtrip.
    """
    if not _CASSETTE_PATH.is_file():
        pytest.skip(
            f"cassette not found at {_CASSETTE_PATH} "
            "(run `python -m ppsspp_dfx_mcp.scripts.record_fixtures` to produce one)",
            allow_module_level=True,
        )
    return _CASSETTE_PATH


# ── Phase 6: real-PPSSPP session-scoped process ──────────────────────────


async def _wait_for_game_loaded(launcher, timeout: float = 30.0) -> None:
    """Poll PPSSPP's game.status until the ISO is loaded.

    The launcher's `_wait_for_port` only confirms the WebSocket port is
    listening — it does NOT wait for PPSSPP to finish loading the ISO.
    On a cold start, PPSSPP takes ~5-15s to mount the ISO and enter the
    game's main loop. Without this wait, the first test that calls
    `memory.read_u32` races PPSSPP's loader and gets
    "CPU not started (level=2)".

    Opens a temporary WsTransport, polls `game.status` every 0.5s until
    the `game` field is a non-None dict (ISO mounted), then closes the
    transport. The caller (real_ppsspp_launcher fixture) re-creates its
    own transport per test via `real_transport`.

    Args:
        launcher: started PpssppLauncher with `.ws_port` set.
        timeout: max seconds to wait for game load (default 30s).

    Raises:
        TimeoutError: if game.status still reports game=None after timeout.
    """
    import time

    from ppsspp_dfx_mcp.core.transport import WsTransport

    port = launcher.ws_port
    assert port is not None, "launcher.ws_port is None — call launcher.start first"

    transport = WsTransport("127.0.0.1", port)
    await transport.connect()
    # send_version may race PPSSPP's handshake; the subsequent
    # game.status poll will retry. Don't fail fixture setup here.
    with contextlib.suppress(Exception):
        await transport.send_version()

    deadline = time.time() + timeout
    last_resp: dict[str, Any] = {}
    try:
        while time.time() < deadline:
            try:
                resp = await transport.call("game.status", timeout=3.0)
                last_resp = resp if isinstance(resp, dict) else {}
                if resp.get("game") is not None:
                    return  # ISO loaded
            except Exception:
                pass  # PPSSPP may briefly reject calls during boot
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"PPSSPP did not load ISO within {timeout}s (last game.status response: {last_resp!r})"
        )
    finally:
        with contextlib.suppress(Exception):
            await transport.close()


@pytest_asyncio.fixture(scope="session")
async def real_ppsspp_launcher(
    ppsspp_exe_path: Path,
    iso_path: Path,
) -> AsyncIterator[Any]:
    """Launch a real PPSSPP process for the whole test session.

    Session-scoped: starts PPSSPP once and reuses it across all tests
    that need a live PPSSPP process (avoiding ~5s startup cost per test).
    The launcher picks a random free port, writes a temporary appendconfig
    ini, and discovers the actual listening port via netstat/ss/lsof.

    Yields:
        PpssppLauncher: the launcher instance (use `.ws_port` to connect).

    Teardown:
        Stops the PPSSPP process gracefully (terminate → kill → force-kill)
        and removes the temporary appendconfig ini.
    """
    from ppsspp_dfx_mcp.core.launcher import PpssppLauncher

    launcher = PpssppLauncher(exe_path=ppsspp_exe_path)
    try:
        await launcher.start(iso_path, wait_seconds=8.0)
        # Wait for ISO load to complete before exposing the launcher to
        # tests. Without this, read_u32/disasm/breakpoint fail with
        # "CPU not started (level=2)".
        await _wait_for_game_loaded(launcher, timeout=30.0)
        yield launcher
    finally:
        launcher.stop()


@pytest.fixture(scope="session")
def real_ppsspp_ws_url(real_ppsspp_launcher) -> str:
    """Return the ws:// URL of the session-scoped PPSSPP process.

    Format: ``ws://127.0.0.1:<port>/debugger``. The port is the launcher's
    discovered actual listening port (may differ from the picked random
    port when ``--appendconfig=`` is ignored on Windows desktop).
    """
    port = real_ppsspp_launcher.ws_port
    assert port is not None, "launcher started but ws_port is None"
    return f"ws://127.0.0.1:{port}/debugger"


@pytest_asyncio.fixture
async def real_transport(real_ppsspp_launcher) -> AsyncIterator[Any]:
    """Yield a connected WsTransport to the live PPSSPP.

    Function-scoped: each test gets a fresh transport (no state leakage
    between tests). The transport performs the version handshake on
    connect. Closed on teardown (best-effort — exceptions swallowed to
    avoid masking the original test failure).

    Usage:
        async with session_client_with_transport(session_id) as (client, t):
            ...
    """
    from ppsspp_dfx_mcp.core.transport import WsTransport

    port = real_ppsspp_launcher.ws_port
    assert port is not None
    transport = WsTransport("127.0.0.1", port)
    await transport.connect()
    await transport.send_version()
    try:
        yield transport
    finally:
        with contextlib.suppress(Exception):  # Best-effort close.
            await transport.close()


@pytest_asyncio.fixture
async def real_session(iso_path: Path, ppsspp_exe_path: Path, monkeypatch):
    """Start a real PPSSPP session via the production session_manager.

    Function-scoped: each test gets its own PPSSPP subprocess (started
    via `session_manager.start_session`) and session_id. The session is
    stopped on teardown via `stop_session`. Use this fixture when the
    test needs to exercise the full session lifecycle (start_session →
    tool calls → stop_session).

    Note: this fixture BYPASSES the session-scoped `real_ppsspp_launcher`
    — it launches its own PPSSPP because `session_manager.start_session`
    internally calls `launcher.start()`. The two fixtures are mutually
    exclusive in intent: `real_ppsspp_launcher` reuses one process
    across many tests (fast); `real_session` launches per test (slow
    but exercises the production path verbatim).

    Env vars applied (via monkeypatch):
        PPSSPP_DFX_TEST_MODE="" (clear, so session_manager uses real launcher)
        PPSSPP_DFX_FIXTURE_DIR="" (clear, no fake fixtures)
        PPSSPP_DFX_EXE_PATH=<ppsspp_exe_path> (so launcher finds the binary;
            the project.yaml default is empty in fresh checkouts and would
            otherwise fall back to a bare "PPSSPPWindows64.exe" filename)
    """
    # Clear test-mode env vars so session_manager takes the real-launcher path.
    monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "")
    monkeypatch.setenv("PPSSPP_DFX_FIXTURE_DIR", "")
    # Set exe path so launcher.start() can locate the binary (PpssppLauncher
    # calls ppsspp_exe_path() which reads this env var first, then project.yaml,
    # then a bare platform-default filename). Without this, start_session fails
    # with PpssppNotFound("PPSSPPWindows64.exe") on fresh checkouts.
    monkeypatch.setenv("PPSSPP_DFX_EXE_PATH", str(ppsspp_exe_path))

    # Reset the session_manager singleton so env vars take effect on the
    # next get_session_manager() call.
    from ppsspp_dfx_mcp.session import session_manager as sm_mod

    sm_mod._default_manager = None
    sm = sm_mod.get_session_manager()

    sess = None
    try:
        sess = await sm.start_session(str(iso_path))
        yield sess.session_id
    finally:
        if sess is not None:
            # Best-effort cleanup; don't mask the original failure.
            with contextlib.suppress(Exception):
                await sm.stop_session(sess.session_id)
        sm_mod._default_manager = None


# ── Phase 6: fake-mode env helper ─────────────────────────────────────────


@pytest.fixture
def fake_mode_env(monkeypatch, fixtures_dir: Path) -> None:
    """Set env vars for fake-mode integration tests.

    Configures the server to substitute FakeTransport + recorded fixtures
    for the real WsTransport. Used by tests that want to exercise the
    full server → tool → transport stack without a live PPSSPP.

    Args:
        fixtures_dir: path to the recorded per-event fixtures directory
            (reuses the mcp_inspector fixture's directory).
    """
    monkeypatch.setenv("PPSSPP_DFX_TEST_MODE", "fake")
    monkeypatch.setenv("PPSSPP_DFX_FIXTURE_DIR", str(fixtures_dir))


# ── Phase 6: real-mode MCP Inspector fixture ──────────────────────────────
#
# Session-scoped real-mode MCP Inspector: launches ONE server subprocess
# in production mode (no PPSSPP_DFX_TEST_MODE) for the whole test session.
# The server uses real WsTransport + session_manager.start_session to
# launch PPSSPP subprocesses on demand.
#
# Tests that use this fixture call ppsspp_session(action=start, iso_path=...)
# to launch a real PPSSPP via the production code path. Each test gets its
# own session_id + PPSSPP subprocess; teardown calls ppsspp_session(stop).
#
# Reuses the anyio cancel_scope workaround from mcp_inspector/conftest.py
# (session-scoped async fixtures have a known incompatibility with anyio
# TaskGroup cancel scopes — see pytest-asyncio#799).


def _is_cancel_scope_teardown_error(exc: BaseException) -> bool:
    """Detect anyio's cross-task cancel_scope RuntimeError (recursively).

    See tests/mcp_inspector/conftest.py for full rationale. We suppress
    ONLY this specific error during teardown; anything else is re-raised.
    """
    if isinstance(exc, RuntimeError) and "cancel scope" in str(exc).lower():
        return True
    sub_exceptions = getattr(exc, "exceptions", None)
    if sub_exceptions:
        return any(_is_cancel_scope_teardown_error(sub) for sub in sub_exceptions)
    return False


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def real_mcp_inspector(
    ppsspp_exe_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[Any]:
    """Launch a real-mode MCP server subprocess and return a ClientSession.

    Session-scoped: the server subprocess is reused across all tests that
    request this fixture. Tests launch their own PPSSPP via
    `ppsspp_session(action=start)` — each test gets a fresh session_id
    and PPSSPP subprocess.

    The server runs in production mode (PPSSPP_DFX_TEST_MODE unset) so
    tool calls route through the real session_manager + launcher +
    WsTransport stack. PPSSPP_DFX_EXE_PATH is set so the launcher uses
    the test PPSSPP executable.

    sessions.json isolation: PPSSPP_DFX_SESSIONS_PATH is set to a
    session-scoped tmp path so the MCP server subprocess writes its
    sessions.json there instead of ``~/.ppsspp-dfx/sessions.json``.
    This avoids sandbox/permission errors (the user's home ``.ppsspp-dfx/``
    dir may be readonly under TRAE Sandbox or CI containers) and prevents
    test runs from polluting the developer's real sessions.json.

    Yields:
        ClientSession: connected, initialized MCP client session.

    Raises:
        pytest.skip: if PPSSPP executable is unavailable (CI-safe).
    """
    import os
    import sys as _sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    # Session-scoped tmp dir for sessions.json (sandbox-safe).
    sessions_tmp_dir = tmp_path_factory.mktemp("ppsspp_dfx_sessions")
    sessions_json = sessions_tmp_dir / "sessions.json"

    # Point the server at the workspace's .ppsspp-dfx/config/ dir (when
    # one exists) so it finds addresses.yaml (game_mode_addr, etc.).
    # Without this, the server looks in CWD/.ppsspp-dfx/config/ — and
    # the server's CWD is the pytest root (this package), not
    # the workspace root. Smoke tests that rely on
    # game_mode_addr would otherwise report "not configured".
    project_config_dir = _workspace_root() / ".ppsspp-dfx" / "config"

    env = os.environ.copy()
    # Production mode: clear test-mode env vars so server uses real WsTransport.
    env.pop("PPSSPP_DFX_TEST_MODE", None)
    env.pop("PPSSPP_DFX_FIXTURE_DIR", None)
    env["PPSSPP_DFX_EXE_PATH"] = str(ppsspp_exe_path)
    env["PPSSPP_DFX_LOG_LEVEL"] = "WARNING"
    # Isolate sessions.json to a session-scoped tmp path. Without this,
    # the MCP server subprocess writes to ~/.ppsspp-dfx/sessions.json
    # which may be blocked by TRAE Sandbox (Permission denied on
    # sessions.json.tmp) or pollute the developer's real session state.
    env["PPSSPP_DFX_SESSIONS_PATH"] = str(sessions_json)
    # Explicit config dir so the server finds addresses.yaml regardless
    # of its CWD. Skip if the project config dir is absent (CI-safe —
    # smoke_test's game_mode_valid check will just fail rather than crash).
    if project_config_dir.is_dir():
        env["PPSSPP_DFX_CONFIG_DIR"] = str(project_config_dir)
    # PYTHONPATH: src/ first (ppsspp_dfx_mcp), then tests/ (FakeTransport
    # + contract_recorder + record_replay — needed for lazy imports even
    # in real mode, in case any code path imports them defensively).
    existing_pp = env.get("PYTHONPATH", "")
    new_pp = os.pathsep.join([str(_SRC_ROOT), str(_TESTS_ROOT)])
    env["PYTHONPATH"] = f"{new_pp}{os.pathsep}{existing_pp}" if existing_pp else new_pp

    server_params = StdioServerParameters(
        command=_sys.executable,
        args=["-m", "ppsspp_dfx_mcp"],
        env=env,
    )

    # ── Manual context-manager entry (not `async with`) ──
    # Same rationale as mcp_inspector/conftest.py: catch anyio's
    # cancel_scope cross-task RuntimeError during teardown so it doesn't
    # surface as a session-finalizer ERROR.
    stdio_cm = stdio_client(server_params)
    read, write = await stdio_cm.__aenter__()
    session_cm = ClientSession(read, write)
    session = await session_cm.__aenter__()
    try:
        await session.initialize()
        yield session
    finally:
        try:
            await session_cm.__aexit__(None, None, None)
        except BaseException as exc:  # noqa: BLE001 — need ExceptionGroup
            if not _is_cancel_scope_teardown_error(exc):
                raise
        try:
            await stdio_cm.__aexit__(None, None, None)
        except BaseException as exc:  # noqa: BLE001
            if not _is_cancel_scope_teardown_error(exc):
                raise


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Path to the recorded real PPSSPP fixtures directory.

    Skips the test session if the fixtures directory is missing (must
    be produced by `python -m ppsspp_dfx_mcp.scripts.record_fixtures`).
    """
    p = _TESTS_ROOT / "cassettes" / "fixtures"
    if not p.is_dir():
        pytest.skip(
            f"fixtures dir not found at {p} "
            "(run `python -m ppsspp_dfx_mcp.scripts.record_fixtures` to produce one)",
            allow_module_level=True,
        )
    return p


@pytest_asyncio.fixture(loop_scope="session")
async def real_mcp_session(real_mcp_inspector, iso_path: Path):
    """Start a real PPSSPP session via the real-mode MCP Inspector.

    Calls ppsspp_session(action=start, iso_path=<real>) through the
    ClientSession → server → session_manager → launcher → PPSSPP path.
    Yields the session_id; teardown calls ppsspp_session(action=stop).

    Function-scoped: each test gets its own PPSSPP subprocess (launched
    by the server's session_manager). The server subprocess is reused
    (session-scoped via real_mcp_inspector); only the PPSSPP process
    is per-test.

    loop_scope="session": MUST match real_mcp_inspector's session loop
    scope. Without this, pytest-asyncio runs the fixture on a function-
    scoped event loop, but ClientSession.call_tool() is bound to the
    session-scoped loop where the ClientSession was created — cross-loop
    calls hang indefinitely (no response, no error).

    Returns:
        str: session_id of the started PPSSPP session.
    """
    import json

    start_result = await real_mcp_inspector.call_tool(
        "ppsspp_session",
        {"action": "start", "iso_path": str(iso_path)},
    )
    if start_result.is_error:
        pytest.fail(f"ppsspp_session(start) failed: {start_result.content!r}")
    start_payload = json.loads(start_result.content[0].text)
    session_id = start_payload["session_id"]
    assert session_id, "ppsspp_session(start) returned empty session_id"

    # Boot readiness (2026-09-06): PPSSPP answers WS requests BEFORE the
    # emulated CPU starts — read_memory/disassemble issued immediately
    # after session start raced the ISO boot and failed with
    # "CPU not started (level=2)". NOTE: game_status's paused=False is a
    # false positive at this stage (observed live), so readiness is
    # probed with the capability the tests actually need: a successful
    # memory read at the top.prx base (60s budget, 1s interval).
    #
    # R18 (2026-09-06): PPSSPP also wedges at launch intermittently (TCP
    # listens, main thread stuck in GPU device creation — review v3
    # finding F-22). A wedged boot never becomes ready, so the poll is
    # wrapped in a recovery loop: on timeout, stop (force-kill) the
    # wedged session and re-launch, at most 2 recoveries, then fail with
    # the real reason instead of poisoning every dependent test.
    async def _read_ready(sid: str) -> bool:
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                r = await real_mcp_inspector.call_tool(
                    "ppsspp_read_memory",
                    {
                        "session_id": sid,
                        "action": "read_u32",
                        "address": "0x08804000",
                    },
                )
                if not r.is_error:
                    return True
            except Exception:
                pass  # transient — session/WS may still be settling
            await asyncio.sleep(1.0)
        return False

    ready = await _read_ready(session_id)
    recoveries = 0
    while not ready and recoveries < 2:
        recoveries += 1
        print(
            f"[boot-recovery {recoveries}] PPSSPP boot not ready in 60s "
            f"— restarting session (wedged launch suspected)"
        )
        with contextlib.suppress(Exception):
            await real_mcp_inspector.call_tool(
                "ppsspp_session",
                {"action": "stop", "session_id": session_id},
            )
        await asyncio.sleep(3.0)
        restart = await real_mcp_inspector.call_tool(
            "ppsspp_session",
            {"action": "start", "iso_path": str(iso_path)},
        )
        if restart.is_error:
            break
        session_id = json.loads(restart.content[0].text)["session_id"]
        ready = await _read_ready(session_id)
    assert ready, (
        "PPSSPP CPU did not start within 60s after "
        f"{recoveries} recovery attempt(s) (boot readiness timeout) — "
        "ISO load may be stalled"
    )

    try:
        yield session_id
    finally:
        with contextlib.suppress(Exception):  # Best-effort cleanup.
            await real_mcp_inspector.call_tool(
                "ppsspp_session",
                {"action": "stop", "session_id": session_id},
            )
