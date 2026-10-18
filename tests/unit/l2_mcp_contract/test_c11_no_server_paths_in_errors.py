"""C-11 lock: tool-error text must not disclose the server's filesystem.

`errors.to_tool_error()` returns a `ToolError` message VERBATIM — by design, it
is the documented contract that pins e.g. `IsoNotFound`'s business text (F-1).
The mitigation therefore cannot live in the redaction layer: it has to be the
raise sites. Three of them interpolated server-derived absolute paths:

  * `ISO path {resolved} is outside PPSSPP_DFX_ISO_ROOT ({root_path})`
  * `ISO file not found: {resolved}`
  * `PPSSPP executable not found: {self.exe_path}`
  * `script file not found for {entry.name!r}: {entry.path}`  (server manifest)

Each is checked here by raising the real error and asserting the absolute path
is absent while the actionable hint survives. The messages must stay useful:
an empty string would "pass" a leak check while failing the user.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ppsspp_dfx_mcp.core.launcher import PpssppLauncher, _validate_iso_path
from ppsspp_dfx_mcp.errors import IsoNotFound, PpssppNotFound, ScriptContractError
from ppsspp_dfx_mcp.tools.script import _load_script_module


def _assert_no_secret(message: str, *secrets: Path | str) -> None:
    for secret in secrets:
        assert str(secret) not in message, f"server path leaked into tool error: {message!r}"


class TestIsoPathErrors:
    def test_outside_root_hides_both_paths(self, tmp_path: Path, monkeypatch) -> None:
        root = tmp_path / "iso-root"
        root.mkdir()
        outside = tmp_path / "elsewhere" / "game.iso"
        monkeypatch.setenv("PPSSPP_DFX_ISO_ROOT", str(root))

        with pytest.raises(IsoNotFound) as exc_info:
            _validate_iso_path(outside)

        message = str(exc_info.value)
        _assert_no_secret(message, root, outside, tmp_path)
        # Still actionable: names the knob that fixes it.
        assert "PPSSPP_DFX_ISO_ROOT" in message

    def test_missing_file_shows_only_the_basename(self, tmp_path: Path) -> None:
        missing = tmp_path / "secret_dir" / "game.iso"

        with pytest.raises(IsoNotFound) as exc_info:
            _validate_iso_path(missing)

        message = str(exc_info.value)
        _assert_no_secret(message, tmp_path, missing.parent)
        assert "game.iso" in message


class TestPpssppExecutableError:
    async def test_missing_executable_hides_its_path(self, tmp_path: Path) -> None:
        exe = tmp_path / "bin" / "PPSSPP.exe"
        launcher = PpssppLauncher(exe_path=exe)
        iso = tmp_path / "game.iso"
        iso.write_bytes(b"\x00" * 16)

        with pytest.raises(PpssppNotFound) as exc_info:
            await launcher.start(iso)

        message = str(exc_info.value)
        _assert_no_secret(message, tmp_path, exe)
        # Stable prefix (existing tests match it) + the knob that fixes it.
        # The rendered text carries a `[PPSSPP_NOT_FOUND] ` code prefix, so
        # match on containment rather than startswith.
        assert "PPSSPP executable not found" in message
        assert "PPSSPP_DFX_EXE_PATH" in message


class _ManifestEntry:
    """Duck-typed ScriptEntry: name/path/normalized_path are all it needs."""

    name = "sess_probe"

    def __init__(self, path: Path) -> None:
        self.path = str(path)

    def normalized_path(self, project_root: Path) -> Path:  # noqa: ARG002 - protocol
        return Path(self.path)


class TestScriptPathError:
    def test_missing_script_hides_the_manifest_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "scripts" / "sess_probe.py"

        with pytest.raises(ScriptContractError) as exc_info:
            _load_script_module(_ManifestEntry(missing), tmp_path)

        message = str(exc_info.value)
        _assert_no_secret(message, tmp_path, missing, missing.parent)
        assert "sess_probe" in message
