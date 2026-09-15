"""test_script_exposure_sync.py — integration: exposed-script sync + session enforcement.

Anchor: spec/script_manifest.py `ScriptEntry.status` (F1) + server.py
`sync_exposed_tools` / `_register_exposed_entry` / `_unregister_exposed_tool`
(F4/F5) + tools/script.py `run_script` session resolution (F2/F3).

Review-r3 fixes covered here:
- F1: machine-readable `status` (explicit > description-prefix inference >
  migrated default); invalid status → ManifestError; exposed+skeleton is
  rejected by the registration preflight (never surfaces in tools/list).
- F2: the exposed wrapper forwards session_id so ctx.session_id is set.
- F3: `requires_ppsspp=true` scripts fail with SESSION_NOT_FOUND when no
  session resolves (tool parameter > Input-model field).
- F4: `sync_exposed_tools` registers newly exposed scripts and unregisters
  removed ones (no server restart needed after reload).
- F5: sync reports declared/registered/added/removed/skipped_skeleton.

Integration scope: real tmp script modules on disk + the real MCP tool
registry (with per-test cleanup so dynamic tools never leak into other
tests).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from ppsspp_dfx_mcp.errors import ManifestError, SessionNotFound
from ppsspp_dfx_mcp.server import (
    _unregister_exposed_tool,
    registered_exposed_names,
    registered_tool_names,
    sync_exposed_tools,
)
from ppsspp_dfx_mcp.spec.script_manifest import (
    ScriptManifest,
    reset_manifest_for_tests,
)

# Session-recording probe script used across the sync/enforcement tests.
# `run` echoes ctx.session_id into the Output so tests can assert the
# F2/F3 session resolution without any PPSSPP dependency.
_PROBE_SCRIPT = '''
from pydantic import BaseModel


class ProbeInput(BaseModel):
    session_id: str | None = None


class ProbeOutput(BaseModel):
    status: str = "ok"
    observed_session_id: str | None = None


async def run(input: ProbeInput, ctx) -> ProbeOutput:
    return ProbeOutput(status="ok", observed_session_id=ctx.session_id)
'''

_PLAIN_SCRIPT = '''
from pydantic import BaseModel


class PlainInput(BaseModel):
    pass


class PlainOutput(BaseModel):
    status: str = "ok"


async def run(input: PlainInput, ctx) -> PlainOutput:
    return PlainOutput(status="ok")
'''


def _write_script(tmp_path: Path, filename: str, source: str) -> str:
    """Write a probe script module and return its absolute path string."""
    script_path = tmp_path / filename
    script_path.write_text(source, encoding="utf-8")
    return str(script_path)


def _write_manifest(tmp_path: Path, entries: list[dict]) -> Path:
    manifest_path = tmp_path / "scripts.manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump({"scripts": entries}, allow_unicode=True),
        encoding="utf-8",
    )
    return manifest_path


def _manifest_entry(name: str, path: str, **overrides) -> dict:
    entry = {
        "name": name,
        "description": "probe script for exposure-sync tests",
        "category": "misc",
        "requires_ppsspp": False,
        "path": path,
        "input_model": "ProbeInput",
        "output_model": "ProbeOutput",
        "exposed": True,
    }
    entry.update(overrides)
    return entry


@pytest.fixture(autouse=True)
def _manifest_and_registry_cleanup():
    """Isolate the manifest singleton and never leak dynamic tools."""
    yield
    for name in list(registered_exposed_names()):
        _unregister_exposed_tool(name)
    from ppsspp_dfx_mcp.tools.script import _clear_module_cache

    _clear_module_cache()
    reset_manifest_for_tests(None)


def _inject_manifest(manifest_path: Path) -> ScriptManifest:
    manifest = ScriptManifest(manifest_path=manifest_path)
    reset_manifest_for_tests(manifest)
    manifest.ensure_loaded()
    return manifest


# ============================================================================
# F1 — status field: explicit > description-prefix inference > default
# ============================================================================


class TestStatusInference:
    """`status` backfill keeps legacy manifests loading unchanged."""

    def test_explicit_status_wins(self, tmp_path: Path):
        """An explicit status field is kept as-is, even with a prefix."""
        path = _write_script(tmp_path, "probe.py", _PROBE_SCRIPT)
        manifest_path = _write_manifest(
            tmp_path,
            [_manifest_entry("probe", path, status="migrated",
                             description="[skeleton] mismatched prefix")],
        )
        manifest = _inject_manifest(manifest_path)
        assert manifest.get_script("probe").status == "migrated"

    def test_skeleton_prefix_infers_skeleton(self, tmp_path: Path):
        """Legacy '[skeleton]' description prefix infers status=skeleton."""
        path = _write_script(tmp_path, "probe.py", _PROBE_SCRIPT)
        manifest_path = _write_manifest(
            tmp_path,
            [_manifest_entry("probe", path,
                             description="[skeleton] body not re-wired")],
        )
        manifest = _inject_manifest(manifest_path)
        assert manifest.get_script("probe").status == "skeleton"

    def test_migrated_prefix_and_no_prefix_default_to_migrated(self, tmp_path: Path):
        """'[migrated]' prefix and plain descriptions both infer migrated."""
        path = _write_script(tmp_path, "probe.py", _PROBE_SCRIPT)
        manifest_path = _write_manifest(
            tmp_path,
            [
                _manifest_entry("probe_a", path,
                                description="[migrated] converted"),
                _manifest_entry("probe_b", path, description="no marker"),
            ],
        )
        manifest = _inject_manifest(manifest_path)
        assert manifest.get_script("probe_a").status == "migrated"
        assert manifest.get_script("probe_b").status == "migrated"

    def test_invalid_status_raises_manifest_error(self, tmp_path: Path):
        """An unknown status value fails manifest validation loudly."""
        path = _write_script(tmp_path, "probe.py", _PROBE_SCRIPT)
        manifest_path = _write_manifest(
            tmp_path, [_manifest_entry("probe", path, status="wip")]
        )
        with pytest.raises(ManifestError, match="invalid status"):
            _inject_manifest(manifest_path)


# ============================================================================
# F1 — exposed preflight rejects skeleton entries
# ============================================================================


class TestSkeletonExposedPreflight:
    """exposed=true + status=skeleton must never become a dynamic tool."""

    def test_skeleton_exposed_not_registered(self, tmp_path: Path, caplog):
        """Skeleton entry is reported in skipped_skeleton, not registered,
        and its module is never imported (path is intentionally bogus)."""
        manifest_path = _write_manifest(
            tmp_path,
            [_manifest_entry(
                "skeleton_probe",
                str(tmp_path / "does_not_matter.py"),
                description="[skeleton] not implemented",
            )],
        )
        _inject_manifest(manifest_path)
        with caplog.at_level(logging.WARNING, logger="ppsspp_dfx_mcp"):
            report = sync_exposed_tools()

        assert report["skipped_skeleton"] == ["skeleton_probe"]
        assert report["failed"] == []
        assert "skeleton_probe" not in registered_exposed_names()
        assert any("status=skeleton" in r.message for r in caplog.records)

    def test_skeleton_exposed_flagged_at_load_time(self, tmp_path: Path, caplog):
        """Loading a manifest with exposed+skeleton logs a warning."""
        manifest_path = _write_manifest(
            tmp_path,
            [_manifest_entry(
                "skeleton_probe",
                str(tmp_path / "does_not_matter.py"),
                description="[skeleton] not implemented",
            )],
        )
        with caplog.at_level(logging.WARNING,
                             logger="ppsspp_dfx_mcp.spec.script_manifest"):
            _inject_manifest(manifest_path)
        assert any(
            "exposed but status=skeleton" in r.message for r in caplog.records
        )


# ============================================================================
# F4 — sync_exposed_tools registers and unregisters
# ============================================================================


class TestSyncExposedTools:
    """Manifest edits reach the dynamic tool registry without restart."""

    def test_register_then_unregister_via_manifest_edit(self, tmp_path: Path):
        """A migrated exposed script registers; removing it unregisters."""
        path = _write_script(tmp_path, "plain.py", _PLAIN_SCRIPT)
        manifest_path = _write_manifest(
            tmp_path,
            [_manifest_entry("sync_probe", path,
                             input_model="PlainInput",
                             output_model="PlainOutput")],
        )
        _inject_manifest(manifest_path)

        report = sync_exposed_tools()
        assert report["added"] == ["sync_probe"]
        assert report["registered"] == 1
        assert "ppsspp_script_sync_probe" in registered_tool_names()

        # Manifest edit drops the entry → next sync unregisters the tool.
        manifest_path.write_text(
            yaml.safe_dump({"scripts": []}), encoding="utf-8"
        )
        _inject_manifest(manifest_path)
        report = sync_exposed_tools()
        assert report["removed"] == ["sync_probe"]
        assert report["registered"] == 0
        assert "ppsspp_script_sync_probe" not in registered_tool_names()

    def test_skeleton_reclassification_unregisters_tool(self, tmp_path: Path):
        """Flipping status migrated→skeleton withdraws an exposed tool."""
        path = _write_script(tmp_path, "plain.py", _PLAIN_SCRIPT)
        manifest_path = _write_manifest(
            tmp_path,
            [_manifest_entry("sync_probe", path, status="migrated",
                             input_model="PlainInput",
                             output_model="PlainOutput")],
        )
        _inject_manifest(manifest_path)
        assert sync_exposed_tools()["registered"] == 1

        _write_manifest(
            tmp_path,
            [_manifest_entry("sync_probe", path, status="skeleton",
                             input_model="PlainInput",
                             output_model="PlainOutput",
                             description="[skeleton] withdrawn")],
        )
        _inject_manifest(manifest_path)
        report = sync_exposed_tools()
        assert report["removed"] == ["sync_probe"]
        assert "sync_probe" not in registered_exposed_names()

    def test_contract_failure_lands_in_failed(self, tmp_path: Path):
        """An exposed script whose module cannot import is reported."""
        manifest_path = _write_manifest(
            tmp_path,
            [_manifest_entry("broken_probe", str(tmp_path / "missing.py"))],
        )
        _inject_manifest(manifest_path)
        report = sync_exposed_tools()
        assert report["failed"] == ["broken_probe"]
        assert "broken_probe" not in registered_exposed_names()


# ============================================================================
# F2/F3 — session resolution: wrapper passthrough + requires_ppsspp
# ============================================================================


class TestSessionResolution:
    """ctx.session_id resolves from tool param > Input field; requires
    is enforced by run_script, not left to scripts."""

    async def test_requires_ppsspp_without_session_raises(self, tmp_path: Path):
        """requires_ppsspp=true + no session → SessionNotFound."""
        from ppsspp_dfx_mcp.tools.script import run_script

        path = _write_script(tmp_path, "probe.py", _PROBE_SCRIPT)
        manifest_path = _write_manifest(
            tmp_path, [_manifest_entry("sess_probe", path,
                                       requires_ppsspp=True)]
        )
        _inject_manifest(manifest_path)

        with pytest.raises(SessionNotFound, match="requires_ppsspp"):
            await run_script(name="sess_probe", input={}, session_id=None)

    async def test_tool_param_session_reaches_ctx(self, tmp_path: Path):
        """A passed session_id lands in ctx.session_id (F2/F3 happy path)."""
        from ppsspp_dfx_mcp.tools.script import run_script

        path = _write_script(tmp_path, "probe.py", _PROBE_SCRIPT)
        manifest_path = _write_manifest(
            tmp_path, [_manifest_entry("sess_probe", path,
                                       requires_ppsspp=True)]
        )
        _inject_manifest(manifest_path)

        result = await run_script(
            name="sess_probe", input={}, session_id="sess-123"
        )
        assert result["output"]["observed_session_id"] == "sess-123"

    async def test_input_field_session_used_when_param_absent(self, tmp_path: Path):
        """Without a tool param, an Input-model session_id field resolves."""
        from ppsspp_dfx_mcp.tools.script import run_script

        path = _write_script(tmp_path, "probe.py", _PROBE_SCRIPT)
        manifest_path = _write_manifest(
            tmp_path, [_manifest_entry("sess_probe", path,
                                       requires_ppsspp=True)]
        )
        _inject_manifest(manifest_path)

        result = await run_script(
            name="sess_probe", input={"session_id": "from-input"}
        )
        assert result["output"]["observed_session_id"] == "from-input"

    async def test_tool_param_wins_over_input_field(self, tmp_path: Path):
        """Priority: explicit tool parameter > Input-model field."""
        from ppsspp_dfx_mcp.tools.script import run_script

        path = _write_script(tmp_path, "probe.py", _PROBE_SCRIPT)
        manifest_path = _write_manifest(
            tmp_path, [_manifest_entry("sess_probe", path,
                                       requires_ppsspp=True)]
        )
        _inject_manifest(manifest_path)

        result = await run_script(
            name="sess_probe",
            input={"session_id": "from-input"},
            session_id="from-param",
        )
        assert result["output"]["observed_session_id"] == "from-param"

    async def test_exposed_wrapper_forwards_session(self, tmp_path: Path):
        """The dynamic-tool wrapper forwards session_id into run_script
        (F2: the exposed path no longer leaves ctx.session_id=None)."""
        from ppsspp_dfx_mcp.server import _build_exposed_wrapper
        from ppsspp_dfx_mcp.tools.script import (
            _get_input_output_models,
            _load_script_module,
            _project_root,
        )
        from ppsspp_dfx_mcp.spec.script_manifest import ScriptEntry

        path = _write_script(tmp_path, "probe.py", _PROBE_SCRIPT)
        manifest_path = _write_manifest(
            tmp_path, [_manifest_entry("sess_probe", path)]
        )
        manifest = _inject_manifest(manifest_path)

        entry = manifest.get_script("sess_probe")
        module = _load_script_module(entry, _project_root())
        input_cls, output_cls = _get_input_output_models(module, entry)
        wrapper = _build_exposed_wrapper(entry, input_cls, output_cls)

        result = await wrapper(session_id="via-wrapper")
        assert result["output"]["observed_session_id"] == "via-wrapper"


# ============================================================================
# F5 — sync report contract
# ============================================================================


class TestSyncReport:
    """The sync report exposes declared-vs-registered reconciliation."""

    def test_report_counts_are_consistent(self, tmp_path: Path):
        """declared counts exposed entries; registered counts live tools;
        skeleton skips are visible instead of silent."""
        ok_path = _write_script(tmp_path, "plain.py", _PLAIN_SCRIPT)
        manifest_path = _write_manifest(
            tmp_path,
            [
                _manifest_entry("ok_probe", ok_path,
                                input_model="PlainInput",
                                output_model="PlainOutput"),
                _manifest_entry(
                    "skel_probe",
                    str(tmp_path / "unused.py"),
                    description="[skeleton] not implemented",
                ),
                _manifest_entry(
                    "broken_probe", str(tmp_path / "missing.py")
                ),
            ],
        )
        _inject_manifest(manifest_path)

        report = sync_exposed_tools()
        assert report["declared"] == 3
        assert report["registered"] == 1
        assert report["added"] == ["ok_probe"]
        assert report["skipped_skeleton"] == ["skel_probe"]
        assert report["failed"] == ["broken_probe"]
        assert report["restart_required"] is False
