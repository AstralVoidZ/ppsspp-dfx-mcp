"""--graphics=vulkan must be the default, and must stay overridable (D16).

D16 root cause: with no backend pinned, PPSSPP auto-detects, picks a
backend that fails, and pops a MODAL "Graphics Error" dialog that blocks
boot until dismissed (measured 31.9s startup, ws_connected=false, every
ticketed call timing out). Pinning Vulkan cut startup to 1.3s and made
ws_connected=true.

So the pin is the DEFAULT, not an opt-in. Two things still have to hold:

  C6.5 the default is applied without any environment variable set
  C6.6 an operator can still override it (PPSSPP_DFX_GPU_ARGS), including
       opting out entirely -- a host without Vulkan must not be forced onto
       it, since ApplyToConfig() assigns g_Config.iGPUBackend
       unconditionally and PPSSPP would then fail to start at all

Deliberately NOT made default: the ini route (graphics_backend). It is
ignored on Windows desktop builds, and now that the command line is pinned
it is redundant there. It is kept because Linux desktop builds DO honour
--appendconfig.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ppsspp_dfx_mcp.session import session_manager as sm

DEFAULT_GPU_ARGS = "--graphics=vulkan"


class TestDefaultBackendIsPinned:
    """C6.5 -- the pin applies with nothing configured."""

    def test_default_is_vulkan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PPSSPP_DFX_GPU_ARGS", raising=False)
        assert sm._env_gpu_args() == [DEFAULT_GPU_ARGS], (
            "the backend pin must be the DEFAULT: without it PPSSPP pops a "
            "modal dialog that blocks boot for ~32s (D16)"
        )

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PPSSPP_DFX_GPU_ARGS", "--graphics=software")
        assert sm._env_gpu_args() == ["--graphics=software"]

    def test_blank_env_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Whitespace-only must not disable the pin by accident."""
        monkeypatch.setenv("PPSSPP_DFX_GPU_ARGS", "   ")
        assert sm._env_gpu_args() == [DEFAULT_GPU_ARGS]


class TestOptOutStillPossible:
    """C6.6 -- a host without Vulkan must not be forced onto it."""

    def test_explicit_none_opts_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PPSSPP_DFX_GPU_ARGS", "none")
        assert sm._env_gpu_args() == [], (
            "'none' must mean 'add no --graphics argument' so a host whose "
            "driver lacks Vulkan can still start PPSSPP"
        )

    def test_opt_out_uses_the_plain_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With no args, launcher.start must be called with no extra_args.

        The zero-argument call shape is what existing test doubles patch,
        so the opt-out has to preserve it exactly.
        """
        import asyncio

        calls: list[tuple] = []

        class _L:
            async def start(self, iso, extra_args=None):
                calls.append((iso, extra_args))
                return "proc"

        monkeypatch.setenv("PPSSPP_DFX_GPU_ARGS", "none")
        got = asyncio.run(sm._start_with_env_args(_L(), "game.iso"))
        assert got == "proc"
        assert calls == [("game.iso", None)], (
            f"opt-out must call start(iso) with no extra_args, got {calls}"
        )

    def test_default_passes_extra_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        calls: list[tuple] = []

        class _L:
            async def start(self, iso, extra_args=None):
                calls.append((iso, extra_args))
                return "proc"

        monkeypatch.delenv("PPSSPP_DFX_GPU_ARGS", raising=False)
        asyncio.run(sm._start_with_env_args(_L(), "game.iso"))
        assert calls == [("game.iso", [DEFAULT_GPU_ARGS])]


class TestIniRouteStillAvailable:
    """The ini pin stays: Linux desktop builds DO honour --appendconfig."""

    def test_graphics_backend_still_parsed(self, monkeypatch) -> None:
        monkeypatch.setenv("PPSSPP_DFX_GRAPHICS_BACKEND", "3")
        assert sm._env_backend() == 3

    def test_ini_writer_emits_graphics_section(self) -> None:
        from ppsspp_dfx_mcp.core.launcher import _write_appendconfig_ini

        ini = _write_appendconfig_ini(1234, 3)
        try:
            text = Path(ini).read_text(encoding="utf-8")
        finally:
            ini.unlink(missing_ok=True)
        assert "GraphicsBackend = 3" in text

    def test_ini_writer_omits_section_by_default(self) -> None:
        from ppsspp_dfx_mcp.core.launcher import _write_appendconfig_ini

        ini = _write_appendconfig_ini(1234)
        try:
            text = Path(ini).read_text(encoding="utf-8")
        finally:
            ini.unlink(missing_ok=True)
        assert "[Graphics]" not in text


class TestEnvDoesNotLeak:
    """A leftover env var must not silently pin the backend elsewhere."""

    def test_no_ppsspp_dfx_gpu_args_in_normal_shell(self) -> None:
        """Sanity: the developer shell must not already carry the var.

        If it did, the 'default' tests above would pass for the wrong reason.
        """
        assert "PPSSPP_DFX_GPU_ARGS" not in os.environ or os.environ.get("PPSSPP_DFX_GPU_ARGS") in (
            "",
            "none",
        ), (
            "PPSSPP_DFX_GPU_ARGS is set in this shell; unset it so the default "
            "behaviour is what actually gets tested"
        )
