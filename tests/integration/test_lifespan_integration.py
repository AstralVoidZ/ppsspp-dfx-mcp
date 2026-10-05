"""test_lifespan_integration.py — integration: registry._lifespan end-to-end.

Anchor: registry.py `_lifespan` async context manager (T049 后的家；
server.py 从 registry 再导出 `_lifespan`/`_load_manifest_and_register_exposed`
/`_shutdown_sessions` 以保持 `ppsspp_dfx_mcp.server` 兼容面).

Integration scope: verify the lifespan startup/shutdown wiring:
- Startup: spawns idle_gc_loop + calls _load_manifest_and_register_exposed
- Shutdown: cancels GC task + calls _shutdown_sessions
- The lifespan is an async context manager (enter → yield → exit)

The existing `test_shutdown_sessions.py` covers _shutdown_sessions
behavior in detail (empty list, N sessions, timeout isolation, failure
isolation). This file covers the higher-level lifespan contract: that
_entering_ and _exiting_ the lifespan triggers the right side effects
in the right order.

T052/T053 补丁点纪律：`_lifespan` 读的是 **registry 模块命名空间**的
绑定，patch `server` 的再导出别名不生效（T049 后实测 3 个集成测试
假阴：mock 调用 0 次）——patch 目标 MUST 是 ppsspp_dfx_mcp.registry。
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

from ppsspp_dfx_mcp import registry as registry_mod
from ppsspp_dfx_mcp.session import session_manager as sm_singleton

# ============================================================================
# Lifespan startup contract
# ============================================================================


class TestLifespanStartup:
    """_lifespan startup calls _load_manifest_and_register_exposed + idle_gc_loop."""

    @staticmethod
    async def _gc_noop():
        """No-op GC loop that yields once then sleeps forever (cancellable)."""
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return

    async def test_lifespan_calls_load_manifest_on_startup(self):
        """Entering _lifespan must call _load_manifest_and_register_exposed."""
        with (
            patch.object(sm_singleton, "idle_gc_loop", self._gc_noop),
            patch.object(registry_mod, "_load_manifest_and_register_exposed") as manifest_mock,
            patch.object(registry_mod, "_shutdown_sessions"),
        ):
            async with registry_mod._lifespan(registry_mod.mcp):
                pass

        manifest_mock.assert_called_once_with()

    async def test_lifespan_starts_idle_gc_loop(self):
        """Entering _lifespan must start the idle_gc_loop background task."""
        gc_started = asyncio.Event()

        async def _gc_signaling():
            gc_started.set()
            try:
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return

        with (
            patch.object(sm_singleton, "idle_gc_loop", _gc_signaling),
            patch.object(registry_mod, "_load_manifest_and_register_exposed"),
            patch.object(registry_mod, "_shutdown_sessions"),
        ):
            async with registry_mod._lifespan(registry_mod.mcp):
                # GC task should have started during lifespan startup.
                await asyncio.wait_for(gc_started.wait(), timeout=1.0)

        assert gc_started.is_set()


# ============================================================================
# Lifespan shutdown contract
# ============================================================================


class TestLifespanShutdown:
    """_lifespan exit calls _shutdown_sessions (regardless of manifest state)."""

    @staticmethod
    async def _gc_noop():
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return

    async def test_lifespan_exit_calls_shutdown_sessions(self):
        """Exiting _lifespan must call _shutdown_sessions exactly once."""
        shutdown_calls = 0

        async def _fake_shutdown():
            nonlocal shutdown_calls
            shutdown_calls += 1

        with (
            patch.object(sm_singleton, "idle_gc_loop", self._gc_noop),
            patch.object(registry_mod, "_load_manifest_and_register_exposed"),
            patch.object(registry_mod, "_shutdown_sessions", _fake_shutdown),
        ):
            async with registry_mod._lifespan(registry_mod.mcp):
                pass  # exit immediately (simulates Ctrl+C right after startup)

        assert shutdown_calls == 1, f"_shutdown_sessions called {shutdown_calls} times, expected 1"

    async def test_lifespan_exit_cancels_gc_task(self):
        """Exiting _lifespan must cancel the idle_gc_loop background task.

        We must let the GC coroutine actually start (enter its try block)
        before exiting the lifespan — otherwise the cancel is delivered
        before the coroutine runs at all, and CancelledError surfaces at
        the function entry (bypassing any try/except inside). The
        `gc_entered_try` event gates our exit on the GC coroutine having
        reached its try block.
        """
        cancellation_requested: dict[str, bool] = {"value": False}
        gc_entered_try = asyncio.Event()

        async def _gc_tracks_cancellation():
            try:
                gc_entered_try.set()
                while True:
                    await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancellation_requested["value"] = True
                raise  # re-raise so task is marked cancelled

        with (
            patch.object(sm_singleton, "idle_gc_loop", _gc_tracks_cancellation),
            patch.object(registry_mod, "_load_manifest_and_register_exposed"),
            patch.object(registry_mod, "_shutdown_sessions"),
        ):
            async with registry_mod._lifespan(registry_mod.mcp):
                # Yield control so the GC task can start and enter its
                # try block before we exit the lifespan.
                await asyncio.wait_for(gc_entered_try.wait(), timeout=1.0)

        # The GC coroutine should have received a CancelledError on lifespan exit.
        assert cancellation_requested["value"] is True, (
            "idle_gc_loop was not cancelled on lifespan exit"
        )


# ============================================================================
# Lifespan robustness: malformed manifest does not abort startup
# ============================================================================


class TestLifespanManifestRobustness:
    """A malformed manifest must NOT abort lifespan startup.

    Anchor: server.py `_lifespan` docstring — "Manifest and exposed-tool
    registration are best-effort: a malformed manifest logs a warning
    but does NOT abort startup (the 3 script tools remain callable;
    `ppsspp_list_scripts` will return an empty list until
    `ppsspp_reload_scripts` succeeds)."
    """

    @staticmethod
    async def _gc_noop():
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return

    async def test_lifespan_does_not_raise_on_manifest_error(self, caplog):
        """A non-`ManifestError` manifest failure must NOT abort startup.

        `_lifespan` wraps the manifest call in `except Exception` so a
        different failure type (e.g. UnicodeDecodeError from a non-UTF8
        scripts.yaml) is logged and startup continues with the static
        tools intact — instead of propagating and killing the server.
        """

        def _failing_manifest_load():
            raise RuntimeError("malformed manifest YAML")

        with (
            patch.object(sm_singleton, "idle_gc_loop", self._gc_noop),
            patch.object(
                registry_mod,
                "_load_manifest_and_register_exposed",
                _failing_manifest_load,
            ),
            patch.object(registry_mod, "_shutdown_sessions"),
            caplog.at_level(logging.WARNING, logger="ppsspp_dfx_mcp"),
        ):
            # Must not raise — a manifest failure is best-effort.
            async with registry_mod._lifespan(registry_mod.mcp):
                pass

        warnings = [r.message for r in caplog.records]
        assert any(
            "manifest load/registration failed" in m and "RuntimeError" in m for m in warnings
        ), f"expected a manifest-failure warning naming the exception type, got: {warnings}"
