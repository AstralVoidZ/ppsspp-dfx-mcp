"""MCP Inspector fixtures — launch server subprocess + ClientSession.

The `mcp_inspector` fixture is session-scoped: it launches ONE server
subprocess for the whole test session and reuses the ClientSession
across tests. This mirrors how real MCP clients (Claude Desktop, etc.)
connect — one server process, many tool calls.

Server subprocess configuration:
- Command: `python -m ppsspp_dfx_mcp` (production entry point)
- Transport: stdio (server reads/writes JSON-RPC on stdin/stdout)
- Env: PPSSPP_DFX_TEST_MODE=fake + PPSSPP_DFX_FIXTURE_DIR=<real fixtures>
- PYTHONPATH: includes src/ (ppsspp_dfx_mcp) + tests/ (FakeTransport,
  contract_recorder) so the fake-mode imports in client_helper resolve.

Cleanup: the ClientSession is closed first (sends JSON-RPC shutdown),
then the stdio_client context closes the subprocess's stdin/stdout,
then the subprocess is terminated. All three steps are in a try/finally
so a crashed test doesn't leak the subprocess.

Scope/loop_scope rationale:
- `scope="session"` — server subprocess is expensive to spawn (~200ms),
  reuse across all mcp_inspector tests.
- `loop_scope="session"` — REQUIRED. pytest_asyncio session-scoped async
  fixtures have a known incompatibility with anyio's TaskGroup cancel
  scopes: session teardown runs in a NEW task (the "session finalizer"
  task) that differs from the setup task. anyio's cancel_scope raises
  `RuntimeError("Attempted to exit cancel scope in a different task
  than it was entered in")` when teardown's task != setup's task
  (see pytest-asyncio#799).

  The workaround (manually enter the stdio_client + ClientSession
  contexts, then catch RuntimeError on teardown) is implemented below.
  The subprocess is killed by stdio_client's internal subprocess context
  manager regardless of whether the cancel_scope exits cleanly — so
  catching the RuntimeError only suppresses the noisy teardown error
  without leaking the subprocess.

  All tests using this fixture MUST declare
  `@pytest.mark.asyncio(loop_scope="session")` so they run on the same
  session-scoped event loop as the fixture.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Resolve paths relative to this conftest.
# conftest.py is at tests/mcp_inspector/conftest.py
# parents[0]=mcp_inspector, parents[1]=tests, parents[2]=ppsspp-dfx-mcp
_HERE = Path(__file__).resolve().parent
_TESTS_ROOT = _HERE.parent  # tests/
_SRC_ROOT = _HERE.parents[1] / "src"  # ppsspp-dfx-mcp/src/
_FIXTURES_DIR = _TESTS_ROOT / "cassettes" / "fixtures"


def _is_cancel_scope_teardown_error(exc: BaseException) -> bool:
    """Detect anyio's cross-task cancel_scope RuntimeError (recursively).

    anyio wraps the RuntimeError in `ExceptionGroup('unhandled errors in
    a TaskGroup', [...])` because it surfaces during TaskGroup teardown.
    We need to walk both the direct exception and any nested
    ExceptionGroup `exceptions` list to find the RuntimeError with the
    telltale message.

    Args:
        exc: The exception to inspect (from __aexit__ or
            run_coroutine_threadsafe.result()).

    Returns:
        True iff the exception (or a nested sub-exception) is the known
        anyio cancel_scope cross-task RuntimeError. This is the ONLY
        exception we suppress — anything else is re-raised.
    """
    if isinstance(exc, RuntimeError) and "cancel scope" in str(exc).lower():
        return True
    # BaseExceptionGroup / ExceptionGroup nests sub-exceptions.
    sub_exceptions = getattr(exc, "exceptions", None)
    if sub_exceptions:
        return any(_is_cancel_scope_teardown_error(sub) for sub in sub_exceptions)
    return False


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def mcp_inspector() -> AsyncIterator[ClientSession]:
    """Launch a fake-mode MCP server subprocess and return a ClientSession.

    Session-scoped: the server subprocess is reused across all tests
    that request this fixture. The ClientSession is initialized
    (handshake complete) before being yielded to the test.

    The server runs with PPSSPP_DFX_TEST_MODE=fake so tool calls that
    need a session will use a FakeTransport + recorded fixtures instead
    of a live PPSSPP WebSocket.

    Implementation note: we manually enter stdio_client + ClientSession
    contexts (rather than using `async with`) so we can catch anyio's
    cancel_scope cross-task RuntimeError during teardown. The
    subprocess is killed by stdio_client's internal subprocess context
    manager regardless of whether the cancel_scope exits cleanly.

    Yields:
        ClientSession: connected, initialized MCP client session.

    Raises:
        RuntimeError: server subprocess failed to start or initialize
            handshake failed (timeout / protocol error).
    """
    if not _FIXTURES_DIR.is_dir():
        pytest.skip(
            f"real PPSSPP fixtures not found at {_FIXTURES_DIR} — "
            "run `python -m ppsspp_dfx_mcp.scripts.record_fixtures` first"
        )

    # Build the environment for the server subprocess.
    env = os.environ.copy()
    env["PPSSPP_DFX_TEST_MODE"] = "fake"
    env["PPSSPP_DFX_FIXTURE_DIR"] = str(_FIXTURES_DIR)
    env["PPSSPP_DFX_LOG_LEVEL"] = "WARNING"  # quiet server logs in tests
    # Isolate the subprocess's sessions.json: a shared real file would (a)
    # let subprocess state leak into tests and (b) make ppsspp_health
    # report 'degraded' when an empty-but-present sessions.json exists.
    # session-scoped fixture → tmp_path_factory (tmp_path is function-scoped).
    _sessions_dir = Path(tempfile.mkdtemp(prefix="ppsspp-dfx-sessions-"))
    env["PPSSPP_DFX_SESSIONS_PATH"] = str(_sessions_dir / "sessions.json")
    # Hermetic config dir: config_dir() resolves from CWD with no upward
    # search, and this subprocess runs with CWD = pytest rootdir, so absent
    # an explicit PPSSPP_DFX_CONFIG_DIR it finds nothing (a clean checkout
    # has no .ppsspp-dfx/). Seed the minimum the wire tests rely on —
    # runtime-band addresses for the completion/complete round-trip — and
    # point the env var at it unconditionally. An earlier revision pointed
    # at a workspace .ppsspp-dfx/config/ found by parent-walking, which
    # silently no-opped in every checkout outside that one workspace.
    _config_dir = Path(tempfile.mkdtemp(prefix="ppsspp-dfx-config-"))
    (_config_dir / "addresses.yaml").write_text(
        # Runtime band 0x08800000..0x0A000000 — see completions._RUNTIME_BANDS.
        "top_base:\n  ppsspp: 0x08804000\ngame_mode_addr: 0x08A0D000\n",
        encoding="utf-8",
    )
    env["PPSSPP_DFX_CONFIG_DIR"] = str(_config_dir)
    # PYTHONPATH: src/ first (ppsspp_dfx_mcp), then tests/ (FakeTransport
    # + contract_recorder + record_replay). The server's
    # _build_fake_transport_for_session imports these lazily, so they
    # must be on the subprocess's import path.
    existing_pp = env.get("PYTHONPATH", "")
    new_pp = os.pathsep.join([str(_SRC_ROOT), str(_TESTS_ROOT)])
    env["PYTHONPATH"] = f"{new_pp}{os.pathsep}{existing_pp}" if existing_pp else new_pp

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ppsspp_dfx_mcp"],
        env=env,
    )

    # ── Manual context-manager entry (not `async with`) ──
    # We need to catch anyio's cancel_scope cross-task RuntimeError
    # during teardown. `async with` would propagate the exception out
    # of the fixture, pytest_asyncio would surface it as a teardown
    # ERROR, and the test session would exit with code 1.
    #
    # By entering manually, we control __aexit__ calls individually and
    # can suppress the known cancel_scope error while re-raising any
    # other exception (e.g. real protocol errors, OS errors, etc.).
    stdio_cm = stdio_client(server_params)
    read, write = await stdio_cm.__aenter__()
    session_cm = ClientSession(read, write)
    session = await session_cm.__aenter__()
    try:
        await session.initialize()
        yield session
    finally:
        # ── Teardown: exit ClientSession first, then stdio_client ──
        # Order matters: ClientSession.__aexit__ sends JSON-RPC shutdown
        # over the streams; stdio_client.__aexit__ closes the streams
        # and kills the subprocess.
        #
        # Both __aexit__ calls may raise anyio's cancel_scope
        # RuntimeError because pytest_asyncio's session-finalizer task
        # != setup task. We catch ONLY that specific error; anything
        # else (real protocol errors, OS errors) is re-raised.
        try:
            await session_cm.__aexit__(None, None, None)
        except BaseException as exc:  # noqa: BLE001 — need to catch ExceptionGroup
            if not _is_cancel_scope_teardown_error(exc):
                raise
        try:
            await stdio_cm.__aexit__(None, None, None)
        except BaseException as exc:  # noqa: BLE001
            if not _is_cancel_scope_teardown_error(exc):
                raise


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to the recorded real PPSSPP fixtures directory."""
    return _FIXTURES_DIR
