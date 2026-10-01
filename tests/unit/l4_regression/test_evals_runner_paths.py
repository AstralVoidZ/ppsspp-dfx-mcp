"""Every path the evals runner resolves must live inside the repository.

`evals/runner.py` carried a monorepo-era constant
`_REPO_ROOT = _PKG_ROOT.parents[1]` and used it for git provenance and for
the default B2 skill directory (`.zcode/skills/ppsspp-dfx`). In a standalone
checkout that path is *outside* the checkout: the default skill dir never
exists (B2 raises), and provenance was read from whatever repository
happened to sit two levels up.

The runner now derives everything from `_PKG_ROOT` (the repository root) and
defaults the skill dir to `<repo>/skills/ppsspp-dfx`.

Run (from the repository root):
    <venv python> -m pytest tests/unit/l4_regression/test_evals_runner_paths.py -q
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[3]
_RUNNER_SRC = _REPO / "evals" / "runner.py"


@pytest.fixture()
def runner(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import evals.runner with the skill-dir override removed.

    Reloaded (not just imported) so the module-level `SKILL_DIR` is
    recomputed under the test's environment even if another test already
    imported the module with the override set.
    """
    monkeypatch.delenv("PPSSPP_DFX_SKILL_DIR", raising=False)
    return importlib.reload(importlib.import_module("evals.runner"))


def test_runner_path_constants_are_repo_relative(runner: Any) -> None:
    assert runner._PKG_ROOT == _REPO, (
        f"runner._PKG_ROOT={runner._PKG_ROOT} != repository root {_REPO}"
    )
    for name in ("_EVALS_DIR", "_SRC_ROOT", "_TESTS_ROOT", "SKILL_DIR"):
        path = Path(getattr(runner, name))
        assert path.is_relative_to(_REPO), f"{name}={path} 落在仓库之外 {_REPO}"


def test_default_skill_dir_points_at_the_shipped_skill(runner: Any) -> None:
    assert runner.SKILL_DIR == _REPO / "skills" / "ppsspp-dfx", (
        f"默认 skill 目录 {runner.SKILL_DIR} 应为 <repo>/skills/ppsspp-dfx"
    )
    assert (runner.SKILL_DIR / "SKILL.md").is_file(), (
        f"{runner.SKILL_DIR / 'SKILL.md'} 不存在——B2 变体会直接报错"
    )


def test_runner_has_no_parent_directory_assumption() -> None:
    """No reference to a directory above the repository root."""
    src = _RUNNER_SRC.read_text(encoding="utf-8")
    assert not re.search(r"_PKG_ROOT\.parents\[", src), (
        "runner.py 仍在对 `_PKG_ROOT` 的父目录取值（monorepo 残留）"
    )
    assert "_REPO_ROOT" not in src, "runner.py 仍保留 `_REPO_ROOT` 常量"
