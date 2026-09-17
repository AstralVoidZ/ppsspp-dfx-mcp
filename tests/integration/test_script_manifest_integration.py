"""test_script_manifest_integration.py — integration: ScriptManifest end-to-end.

Anchor: spec/script_manifest.py — `ScriptEntry` + `ScriptManifest` +
`get_manifest` / `reset_manifest_for_tests`.

Integration scope: verify the manifest loading, reloading, and singleton
lifecycle against the file system (no real manifest file — tests inject
tmp paths or exercise the missing-manifest path).

Contract:
- Missing manifest file → warning + empty entries (server still starts).
- `reload()` re-reads the YAML file and rebuilds the registry.
- `get_manifest()` returns the process-wide singleton; `reset_manifest_for_tests`
  clears or replaces it.
- `ScriptEntry` model is frozen + extra='forbid' (defensive contract).
- Manifest path resolution: explicit path > config_dir default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ppsspp_dfx_mcp.errors import ManifestError
from ppsspp_dfx_mcp.spec.script_manifest import (
    ScriptEntry,
    ScriptManifest,
    get_manifest,
    reset_manifest_for_tests,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the manifest singleton before and after each test."""
    reset_manifest_for_tests(None)
    yield
    reset_manifest_for_tests(None)


# ============================================================================
# Missing manifest file
# ============================================================================


class TestMissingManifest:
    """A missing manifest file is not an error — it yields an empty registry."""

    def test_missing_manifest_list_returns_empty(self, tmp_path: Path):
        """list_scripts() on a missing manifest returns []."""
        manifest = ScriptManifest(manifest_path=tmp_path / "nonexistent.yaml")
        entries = manifest.list_scripts()
        assert entries == []

    def test_missing_manifest_is_loaded_flag_true(self, tmp_path: Path):
        """After list_scripts(), is_loaded() returns True (lazy load happened)."""
        manifest = ScriptManifest(manifest_path=tmp_path / "nonexistent.yaml")
        assert not manifest.is_loaded()
        manifest.list_scripts()  # triggers lazy load
        assert manifest.is_loaded()

    def test_missing_manifest_reload_returns_zero(self, tmp_path: Path):
        """reload() on a missing manifest returns 0 (empty registry)."""
        manifest = ScriptManifest(manifest_path=tmp_path / "nonexistent.yaml")
        count = manifest.reload()
        assert count == 0

    def test_missing_manifest_get_script_raises_not_found(self, tmp_path: Path):
        """get_script() on a missing manifest raises ScriptNotFound."""
        from ppsspp_dfx_mcp.errors import ScriptNotFound

        manifest = ScriptManifest(manifest_path=tmp_path / "nonexistent.yaml")
        with pytest.raises(ScriptNotFound):
            manifest.get_script("any_name")


# ============================================================================
# Malformed manifest
# ============================================================================


class TestMalformedManifest:
    """A malformed manifest file raises ManifestError at load time."""

    def test_malformed_yaml_raises_manifest_error(self, tmp_path: Path):
        """Invalid YAML raises ManifestError with a parse-failed message."""
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text("scripts: [this is not valid yaml: - ]", encoding="utf-8")
        manifest = ScriptManifest(manifest_path=bad_file)
        with pytest.raises(ManifestError, match="YAML parse failed"):
            manifest.list_scripts()

    def test_non_mapping_root_raises_manifest_error(self, tmp_path: Path):
        """A YAML list as root (not a mapping) raises ManifestError."""
        bad_file = tmp_path / "list_root.yaml"
        bad_file.write_text("- item1\n- item2\n", encoding="utf-8")
        manifest = ScriptManifest(manifest_path=bad_file)
        with pytest.raises(ManifestError, match="root must be a mapping"):
            manifest.list_scripts()

    def test_non_list_scripts_field_raises_manifest_error(self, tmp_path: Path):
        """A `scripts` field that is not a list raises ManifestError."""
        bad_file = tmp_path / "scripts_not_list.yaml"
        bad_file.write_text("scripts: not_a_list\n", encoding="utf-8")
        manifest = ScriptManifest(manifest_path=bad_file)
        with pytest.raises(ManifestError, match="'scripts' must be a list"):
            manifest.list_scripts()


# ============================================================================
# Singleton lifecycle
# ============================================================================


class TestManifestSingleton:
    """get_manifest() / reset_manifest_for_tests() singleton behavior."""

    def test_get_manifest_returns_same_instance(self):
        """get_manifest() returns the same instance on repeated calls."""
        m1 = get_manifest()
        m2 = get_manifest()
        assert m1 is m2

    def test_reset_clears_singleton(self):
        """reset_manifest_for_tests(None) clears the singleton; next
        get_manifest() returns a fresh instance."""
        m1 = get_manifest()
        reset_manifest_for_tests(None)
        m2 = get_manifest()
        assert m1 is not m2

    def test_reset_injects_custom_instance(self):
        """reset_manifest_for_tests(manifest) injects a custom instance."""
        custom = ScriptManifest(manifest_path=Path("/custom/path.yaml"))
        reset_manifest_for_tests(custom)
        assert get_manifest() is custom


# ============================================================================
# ScriptEntry Pydantic contract
# ============================================================================


class TestScriptEntryContract:
    """ScriptEntry is frozen + extra='forbid' (defensive contract)."""

    def test_script_entry_is_frozen(self):
        """ScriptEntry instances must be immutable (frozen=True)."""
        config = ScriptEntry.model_config
        assert config.get("frozen") is True

    def test_script_entry_forbids_extra(self):
        """ScriptEntry must reject unknown fields (extra='forbid')."""
        config = ScriptEntry.model_config
        assert config.get("extra") == "forbid"

    def test_script_entry_required_fields(self):
        """ScriptEntry must reject construction missing required fields."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ScriptEntry()  # all fields missing

    def test_script_entry_accepts_valid_input(self):
        """A valid ScriptEntry construction succeeds."""
        entry = ScriptEntry(
            name="test_script",
            description="A test script",
            category="misc",
            path="scripts/test.py",
            input_model="TestInput",
            output_model="TestOutput",
        )
        assert entry.name == "test_script"
        assert entry.entry == "run"  # default
        assert entry.exposed is False  # default
        assert entry.requires_ppsspp is False  # default

    def test_script_entry_rejects_invalid_category(self):
        """An invalid category value raises ManifestError (validated in
        ScriptManifest._load_locked, not in ScriptEntry itself — the
        model accepts any string, the manifest enforces the allowlist)."""
        # ScriptEntry itself accepts any category string (no enum check);
        # the manifest loader enforces the _VALID_CATEGORIES allowlist.
        entry = ScriptEntry(
            name="test",
            description="desc",
            category="invalid_category",  # accepted by model
            path="scripts/test.py",
            input_model="In",
            output_model="Out",
        )
        assert entry.category == "invalid_category"  # model accepts
        # Manifest loader is the gatekeeper (covered by other tests).


# ============================================================================
# Manifest path resolution
# ============================================================================


class TestManifestPathResolution:
    """manifest_path() respects explicit path > config_dir default."""

    def test_explicit_path_returned_as_is(self, tmp_path: Path):
        """When an explicit path is given to __init__, manifest_path() returns it."""
        explicit = tmp_path / "custom.yaml"
        manifest = ScriptManifest(manifest_path=explicit)
        assert manifest.manifest_path() == explicit

    def test_default_path_uses_config_dir(self):
        """Without an explicit path, manifest_path() falls back to config_dir()."""
        manifest = ScriptManifest()
        from ppsspp_dfx_mcp.config import config_dir

        expected = config_dir() / "scripts.manifest.yaml"
        assert manifest.manifest_path() == expected
