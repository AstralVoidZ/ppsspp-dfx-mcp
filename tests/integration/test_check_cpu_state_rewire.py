"""test_check_cpu_state_rewire.py — integration: check_cpu_state rewired body.

Anchor: tools/topx_diagnostics/state/check_cpu_state.py (rewired per
) + tools/script.py F3
requires_ppsspp enforcement + server.py sync_exposed_tools (F4/F5).

Covers the plan's acceptance assertions that are testable without a real
PPSSPP instance:
- no session + requires_ppsspp=true → SESSION_NOT_FOUND (F3, not the old
  not_implemented skeleton response);
- running / paused / freeze_suspected decision matrix, with the GPU/PC/
  memory tool modules patched (deferred imports inside run() make those
  patch points stable);
- game_mode degrades to None when game_mode_addr is not configured
  (no hardcoded addresses — plan §2/§6);
- the manifest entry (status=migrated, exposed=true) registers as a
  dynamic tool via sync_exposed_tools.

The real-machine three-state smoke test (running/paused against live
PPSSPP) is out of CI scope per the plan §5 S4.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from ppsspp_dfx_mcp.errors import CpuStateError, SessionNotFound
from ppsspp_dfx_mcp.server import (
    _unregister_exposed_tool,
    registered_exposed_names,
    sync_exposed_tools,
)
from ppsspp_dfx_mcp.spec.script_manifest import (
    ScriptManifest,
    reset_manifest_for_tests,
)
from ppsspp_dfx_mcp.tools.script import run_script

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT_PATH = _REPO_ROOT / "tools" / "topx_diagnostics" / "state" / "check_cpu_state.py"

# The rewired script is a dev-workspace artifact (TransLens tools/), not
# tracked in this repo — CI checkouts can't see it. Skip the whole module
# there; the assert in _inject_manifest stays as a guard against path rot.
pytestmark = pytest.mark.skipif(
    not _SCRIPT_PATH.exists(),
    reason=f"workspace rewired script not present: {_SCRIPT_PATH}",
)

# Patch points: deferred imports inside run() resolve these attributes at
# call time, so patching the source modules is stable.
_GPU_PROBE = "ppsspp_dfx_mcp.tools.gpu_stats.gpu_stats"
_GET_PC = "ppsspp_dfx_mcp.tools.query.get_pc"
_READ_MEM = "ppsspp_dfx_mcp.tools.memory.read_memory"
_ADDRESSES = "ppsspp_dfx_mcp.tools.script._addresses"

_GAME_MODE_ADDR = "0x08A0D000"


def _gpu_error(stepping0: bool, stepping1: bool) -> CpuStateError:
    """A CpuStateError shaped like the real _require_running message."""
    return CpuStateError(
        "gpu.stats.get requires CPU running (not stepping): "
        f"[stepping0={stepping0}, stepping1={stepping1}, "
        "ticks0=15636859008, ticks1=15636859008, 50ms probe]"
    )


@pytest.fixture(autouse=True)
def _manifest_and_registry_cleanup():
    """Isolate the manifest singleton; never leak dynamic tools."""
    yield
    for name in list(registered_exposed_names()):
        _unregister_exposed_tool(name)
    from ppsspp_dfx_mcp.tools.script import _clear_module_cache

    _clear_module_cache()
    reset_manifest_for_tests(None)


def _inject_manifest(tmp_path: Path) -> ScriptManifest:
    """Point the manifest singleton at the REAL rewired script file.

    The manifest YAML lives in `tmp_path` (never in the repo tree); only
    the script `path` points at the real file (absolute-path escape
    hatch documented in ScriptEntry.normalized_path).
    """
    assert _SCRIPT_PATH.exists(), f"rewired script missing: {_SCRIPT_PATH}"
    manifest_path = tmp_path / "check_cpu_state.manifest.test.yaml"
    manifest_path.write_text(
        yaml.safe_dump(
            {
                "scripts": [
                    {
                        "name": "check_cpu_state",
                        "description": "[migrated] probe entry for tests",
                        "category": "state",
                        "requires_ppsspp": True,
                        "status": "migrated",
                        "path": str(_SCRIPT_PATH),
                        "input_model": "CheckCpuStateInput",
                        "output_model": "CheckCpuStateOutput",
                        "entry": "run",
                        "exposed": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = ScriptManifest(manifest_path=manifest_path)
    reset_manifest_for_tests(manifest)
    manifest.ensure_loaded()
    return manifest


# ============================================================================
# F3 — requires_ppsspp enforcement replaces the skeleton response
# ============================================================================


class TestRequiresPpssppEnforcement:
    async def test_no_session_raises_session_not_found(self, tmp_path: Path):
        """No session → SESSION_NOT_FOUND, NOT the old not_implemented."""
        _inject_manifest(tmp_path)
        with pytest.raises(SessionNotFound, match="requires_ppsspp"):
            await run_script(name="check_cpu_state", input={}, session_id=None)


# ============================================================================
# Decision matrix (tool modules patched)
# ============================================================================


class TestDecisionMatrix:
    async def test_running_state_reports_fps_pc_game_mode(self, tmp_path: Path):
        """gpu_stats succeeds → running + fps + high-trust PC + game_mode."""
        _inject_manifest(tmp_path)
        gpu = AsyncMock(return_value={"fps": 30.0, "vblanks_per_second": 59.9})
        pc = AsyncMock(return_value={"pc": "0x088EF0F4", "trust_level": "high"})
        mem = AsyncMock(return_value={"value": 3})
        with patch(_GPU_PROBE, gpu), \
             patch(_GET_PC, pc), \
             patch(_READ_MEM, mem), \
             patch(_ADDRESSES, return_value={"game_mode_addr": _GAME_MODE_ADDR}):
            result = await run_script(
                name="check_cpu_state", input={}, session_id="sess-1"
            )

        output = result["output"]
        assert output["status"] == "ok"
        assert output["cpu_state"] == "running"
        assert output["pc"] == "0x088EF0F4"
        assert output["game_mode"] == 3
        assert output["fps"] == 30.0
        assert output["freeze_suspected"] is False
        # game_mode read uses the addresses.yaml address, verbatim format
        mem.assert_awaited_once_with(
            action="read_u32", address=_GAME_MODE_ADDR, session_id="sess-1"
        )

    async def test_paused_state_when_stepping(self, tmp_path: Path):
        """CpuStateError with stepping=True → paused (breakpoint hit)."""
        _inject_manifest(tmp_path)
        gpu = AsyncMock(side_effect=_gpu_error(stepping0=True, stepping1=True))
        pc = AsyncMock(return_value={"pc": "0x088EF0F8", "trust_level": "high"})
        mem = AsyncMock(return_value={"value": 0})
        with patch(_GPU_PROBE, gpu), \
             patch(_GET_PC, pc), \
             patch(_READ_MEM, mem), \
             patch(_ADDRESSES, return_value={"game_mode_addr": _GAME_MODE_ADDR}):
            result = await run_script(
                name="check_cpu_state", input={}, session_id="sess-1"
            )

        output = result["output"]
        assert output["cpu_state"] == "paused"
        assert output["freeze_suspected"] is False
        assert output["pc"] == "0x088EF0F8"
        assert "Do NOT stop the session" in output["message"]

    async def test_freeze_suspected_when_ticks_frozen(self, tmp_path: Path):
        """CpuStateError with stepping=False → freeze_suspected workflow."""
        _inject_manifest(tmp_path)
        gpu = AsyncMock(side_effect=_gpu_error(stepping0=False, stepping1=False))
        pc = AsyncMock(return_value={"pc": "0x088EF0F4", "trust_level": "high"})
        with patch(_GPU_PROBE, gpu), \
             patch(_GET_PC, pc), \
             patch(_READ_MEM, AsyncMock(return_value={"value": 0})), \
             patch(_ADDRESSES, return_value={"game_mode_addr": _GAME_MODE_ADDR}):
            result = await run_script(
                name="check_cpu_state", input={}, session_id="sess-1"
            )

        output = result["output"]
        assert output["cpu_state"] == "freeze_suspected"
        assert output["freeze_suspected"] is True
        assert "CPU_FREEZE_SUSPECTED workflow" in output["message"]

    async def test_running_but_no_flips_flags_freeze(self, tmp_path: Path):
        """fps present but vblanks ~0 → freeze_suspected on the running path."""
        _inject_manifest(tmp_path)
        gpu = AsyncMock(return_value={"fps": 0.0, "vblanks_per_second": 0.0})
        with patch(_GPU_PROBE, gpu), \
             patch(_GET_PC, AsyncMock(return_value={"pc": "0x088EF0F4"})), \
             patch(_READ_MEM, AsyncMock(return_value={"value": 0})), \
             patch(_ADDRESSES, return_value={"game_mode_addr": _GAME_MODE_ADDR}):
            result = await run_script(
                name="check_cpu_state", input={}, session_id="sess-1"
            )

        output = result["output"]
        assert output["cpu_state"] == "freeze_suspected"
        assert output["freeze_suspected"] is True


# ============================================================================
# Degraded facts — missing config degrades, never fabricates
# ============================================================================


class TestDegradedFacts:
    async def test_missing_game_mode_addr_returns_none(self, tmp_path: Path):
        """No game_mode_addr in addresses.yaml → game_mode=None (no read)."""
        _inject_manifest(tmp_path)
        gpu = AsyncMock(return_value={"fps": 30.0, "vblanks_per_second": 59.9})
        mem = AsyncMock(side_effect=AssertionError("read_memory must not be called"))
        with patch(_GPU_PROBE, gpu), \
             patch(_GET_PC, AsyncMock(return_value={"pc": "0x088EF0F4"})), \
             patch(_READ_MEM, mem), \
             patch(_ADDRESSES, return_value={}):
            result = await run_script(
                name="check_cpu_state", input={}, session_id="sess-1"
            )

        output = result["output"]
        assert output["cpu_state"] == "running"
        assert output["game_mode"] is None
        mem.assert_not_awaited()


# ============================================================================
# F4/F5 — the rewired entry registers as a dynamic tool
# ============================================================================


class TestExposure:
    def test_sync_registers_rewired_script(self, tmp_path: Path):
        """status=migrated + exposed=true → sync registers the tool."""
        _inject_manifest(tmp_path)
        report = sync_exposed_tools()
        assert report["added"] == ["check_cpu_state"]
        assert "check_cpu_state" in registered_exposed_names()
