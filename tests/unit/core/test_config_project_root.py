"""`config.project_root()` — env var > cwd resolution with marker detection.

Tests the cwd → env var refactor (issue: non-standard cwd configuration):
- PPSSPP_DFX_PROJECT_ROOT env var > cwd (env var pins the root regardless
  of the MCP host's spawn directory).
- Env var set but path missing → ConfigInvalid (explicit config errors
  fail immediately; source: cwd-config-refactor experience).
- Env var set but path is a file (not dir) → ConfigInvalid.
- Env var set, path valid, no .ppsspp-dfx/ marker → warning (not blocking).
- Env var unset, cwd has no .ppsspp-dfx/ marker → warning (not blocking,
  backward compat).
- Env var unset, cwd has .ppsspp-dfx/ marker → no warning, returns cwd.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ppsspp_dfx_mcp import config


@pytest.fixture(autouse=True)
def _reset_project_root_warning_flag():
    """Reset the module-level warning guard between tests.

    project_root() uses a module-level flag to avoid repeat warnings.
    Each test needs a fresh flag to assert warning behavior independently.
    """
    config._project_root_marker_warned = False
    yield
    config._project_root_marker_warned = False


class TestProjectRootEnvVarOverride:
    """PPSSPP_DFX_PROJECT_ROOT env var overrides cwd."""

    def test_env_var_pins_project_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When env var is set to a valid dir with marker, returns that dir."""
        (tmp_path / ".ppsspp-dfx").mkdir()
        monkeypatch.setenv("PPSSPP_DFX_PROJECT_ROOT", str(tmp_path))
        result = config.project_root()
        assert result == tmp_path.resolve()

    def test_env_var_takes_priority_over_cwd(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Env var wins even when cwd has its own .ppsspp-dfx/ marker."""
        (tmp_path / ".ppsspp-dfx").mkdir()
        other = tmp_path / "other_root"
        other.mkdir()
        (other / ".ppsspp-dfx").mkdir()
        monkeypatch.setenv("PPSSPP_DFX_PROJECT_ROOT", str(other))
        result = config.project_root()
        assert result == other.resolve()
        assert result != Path.cwd().resolve()

    def test_env_var_expands_user(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Env var with ~ is expanded to the home directory."""
        home = Path.home()
        monkeypatch.setenv("PPSSPP_DFX_PROJECT_ROOT", str(home))
        result = config.project_root()
        assert result == home.resolve()


class TestProjectRootEnvVarInvalid:
    """Env var set but invalid → ConfigInvalid (explicit config error)."""

    def test_env_var_nonexistent_path_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Env var pointing to a nonexistent path raises ConfigInvalid."""
        from ppsspp_dfx_mcp.errors import ConfigInvalid

        missing = tmp_path / "does_not_exist"
        monkeypatch.setenv("PPSSPP_DFX_PROJECT_ROOT", str(missing))
        with pytest.raises(ConfigInvalid, match="does not exist"):
            config.project_root()

    def test_env_var_pointing_at_file_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Env var pointing at a file (not a directory) raises ConfigInvalid."""
        from ppsspp_dfx_mcp.errors import ConfigInvalid

        file_path = tmp_path / "not_a_dir.txt"
        file_path.write_text("hello")
        monkeypatch.setenv("PPSSPP_DFX_PROJECT_ROOT", str(file_path))
        with pytest.raises(ConfigInvalid, match="not a directory"):
            config.project_root()

    def test_env_var_error_message_mentions_unset_hint(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Error message guides the user to unset the env var or fix the path."""
        from ppsspp_dfx_mcp.errors import ConfigInvalid

        missing = tmp_path / "does_not_exist"
        monkeypatch.setenv("PPSSPP_DFX_PROJECT_ROOT", str(missing))
        with pytest.raises(ConfigInvalid) as exc_info:
            config.project_root()
        msg = str(exc_info.value)
        assert "unset" in msg.lower() or "fix" in msg.lower(), (
            f"error should hint at unset/fix; got: {msg!r}"
        )


class TestProjectRootMarkerWarning:
    """Marker directory detection (.ppsspp-dfx/) — warning, not blocking."""

    def test_env_var_valid_no_marker_warns_not_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Env var set, path valid, but no .ppsspp-dfx/ → warning, not error."""
        monkeypatch.setenv("PPSSPP_DFX_PROJECT_ROOT", str(tmp_path))
        with caplog.at_level("WARNING", logger="ppsspp_dfx_mcp"):
            result = config.project_root()
        assert result == tmp_path.resolve()
        assert any("marker" in r.message.lower() for r in caplog.records), (
            "missing marker dir should emit a warning"
        )

    def test_cwd_no_marker_warns_not_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Env var unset, cwd has no .ppsspp-dfx/ → warning, not error."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PPSSPP_DFX_PROJECT_ROOT", raising=False)
        config._project_root_marker_warned = False
        with caplog.at_level("WARNING", logger="ppsspp_dfx_mcp"):
            result = config.project_root()
        assert result == tmp_path.resolve()
        assert any("marker" in r.message.lower() for r in caplog.records), (
            "cwd without marker dir should emit a warning"
        )

    def test_cwd_with_marker_no_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Env var unset, cwd has .ppsspp-dfx/ → no warning."""
        (tmp_path / ".ppsspp-dfx").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PPSSPP_DFX_PROJECT_ROOT", raising=False)
        config._project_root_marker_warned = False
        with caplog.at_level("WARNING", logger="ppsspp_dfx_mcp"):
            result = config.project_root()
        assert result == tmp_path.resolve()
        assert not any("marker" in r.message.lower() for r in caplog.records), (
            "cwd with marker dir should not emit a warning"
        )

    def test_warning_emitted_at_most_once(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Repeated calls do not repeat the marker-missing warning."""
        monkeypatch.setenv("PPSSPP_DFX_PROJECT_ROOT", str(tmp_path))
        config._project_root_marker_warned = False
        with caplog.at_level("WARNING", logger="ppsspp_dfx_mcp"):
            config.project_root()
            config.project_root()
            config.project_root()
        marker_warnings = [r for r in caplog.records if "marker" in r.message.lower()]
        assert len(marker_warnings) == 1, (
            f"marker warning should fire at most once; got {len(marker_warnings)}"
        )


class TestProjectRootCwdFallback:
    """Env var unset → cwd fallback (backward compat)."""

    def test_env_var_unset_returns_cwd(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """When env var is unset, project_root() returns cwd."""
        (tmp_path / ".ppsspp-dfx").mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("PPSSPP_DFX_PROJECT_ROOT", raising=False)
        config._project_root_marker_warned = False
        result = config.project_root()
        assert result == tmp_path.resolve()
