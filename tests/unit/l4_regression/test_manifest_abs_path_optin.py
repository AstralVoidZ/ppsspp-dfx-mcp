"""L4 regression: W8 (code review v3) — absolute manifest path opt-in.

``ScriptEntry.normalized_path`` used to return ANY absolute ``path`` from
the manifest after only rejecting ``..`` and non-``.py`` suffixes — a
tampered ``scripts.manifest.yaml`` could therefore load and execute an
arbitrary Python file on disk. The absolute branch now requires the
explicit ``PPSSPP_DFX_ALLOW_ABS_SCRIPT=1`` opt-in; the relative branch's
containment check is unchanged.

Contract:
- absolute path + no env  → ManifestError naming the env var.
- absolute path + env=1   → returned as-is (fixtures / dev rewiring).
- the opt-in does NOT relax ``..`` or the ``.py`` suffix rules.
- relative paths keep working without the env (containment still enforced).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ppsspp_dfx_mcp.errors import ManifestError
from ppsspp_dfx_mcp.spec.script_manifest import ScriptEntry

_ENV = "PPSSPP_DFX_ALLOW_ABS_SCRIPT"


def _entry(path: str) -> ScriptEntry:
    return ScriptEntry(
        name="abs_script",
        description="absolute-path fixture",
        category="misc",
        path=path,
        input_model="AbsInput",
        output_model="AbsOutput",
    )


def test_absolute_path_without_optin_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(_ENV, raising=False)
    entry = _entry(str(tmp_path / "outside.py"))

    with pytest.raises(ManifestError, match=_ENV):
        entry.normalized_path(tmp_path / "project")


def test_absolute_path_with_optin_is_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(_ENV, "1")
    target = tmp_path / "outside.py"
    entry = _entry(str(target))

    assert entry.normalized_path(tmp_path / "project") == target


def test_absolute_path_optin_still_rejects_parent_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(_ENV, "1")
    entry = _entry(str(Path(tmp_path, "..", "evil.py")))

    with pytest.raises(ManifestError, match=r"\.\."):
        entry.normalized_path(tmp_path)


def test_absolute_path_optin_still_requires_py_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(_ENV, "1")
    entry = _entry(str(tmp_path / "evil.txt"))

    with pytest.raises(ManifestError, match=r"\.py"):
        entry.normalized_path(tmp_path)


def test_relative_path_containment_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Relative paths need no opt-in; escape attempts are still refused."""
    monkeypatch.delenv(_ENV, raising=False)
    root = tmp_path / "project"
    root.mkdir()

    assert _entry("scripts/ok.py").normalized_path(root) == root / "scripts" / "ok.py"
    with pytest.raises(ManifestError, match="escapes project root"):
        _entry("../outside.py").normalized_path(root)
