"""L4/W11 + specs-010 L4 guard: every committed `.mcp.json` must be launchable
on any platform and must not pin a platform-bound interpreter path.

History (two rounds, both kept here so the rule is not re-litigated by accident):

* **W11 (review v4)** — the audit found the package-local
  `mcps/ppsspp-dfx-mcp/.mcp.json` pointing at
  `.venv/ppsspp-dfx-mcp/Scripts/python.exe`: a relative path that only resolved
  when the client happened to run from the workspace root, and that used the
  Windows-only venv layout (`Scripts/` vs POSIX `bin/`). `check_env.py` policed
  only `<workspace>/.mcp.json`, so the package config drifted unpoliced.
* **specs/010 L4** — the fix for W11 (`venv interpreter beside the config`) was
  still platform-bound, and it also required provisioning a venv *per config*.
  The committed form is now the **general runner** (`uv run …`), which is
  platform-neutral and self-provisioning.

Contract asserted below (specs/010 FR-008/FR-009/FR-010, contracts/local-launch.md):
`command` is the runner and resolvable on PATH; `args` is one of exactly two
self-sufficient forms; no platform-bound interpreter path; no debug-level log
default injected.

These tests exercise the pure helpers (`validate_mcp_entry` / `runner_available`
/ `_runner_args_error` / `mcp_config_dirs`) and the `bootstrap` wiring, against
tmp dirs with the script's root globals monkeypatched — no real venv and no real
runner are required.

Run (from the repository root):
    <venv python> -m pytest tests/unit/l4_regression/test_w11_mcp_config_validation.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO / "scripts"


def _load_check_env() -> Any:
    """Load `scripts/check_env.py` by path (it does `from _wire import ...`)."""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("check_env_w11", _SCRIPTS / "check_env.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def check_env() -> Any:
    mod = _load_check_env()
    # The tests below assert messages, not the host's PATH. Pin the runner as
    # available unless a test overrides it, so results do not depend on whether
    # `uv` happens to be installed on the machine running the suite.
    mod.runner_available = lambda: "/fake/path/uv"  # type: ignore[assignment]
    return mod


def _entry(command: str = "uv", args: Any = None, env: Any = None) -> dict[str, Any]:
    inner: dict[str, Any] = {
        "command": command,
        "args": ["run", "ppsspp-dfx-mcp"] if args is None else args,
    }
    if env is not None:
        inner["env"] = env
    return {"mcpServers": {"ppsspp-dfx": inner}}


def _pkg_root(tmp_path: Path, name: str = "pkg") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return root


# ── the two accepted forms ────────────────────────────────────────────────
def test_self_locating_runner_entry_validates_clean(check_env: Any, tmp_path: Path) -> None:
    """Config sitting at the package root: the runner finds the project by cwd."""
    config_dir = _pkg_root(tmp_path)
    assert check_env.validate_mcp_entry(config_dir, _entry()) is None


def test_directory_pinned_entry_validates_clean(check_env: Any, tmp_path: Path) -> None:
    """Config NOT at the package root (e.g. the workspace root): must pin it."""
    config_dir = tmp_path / "ws"
    config_dir.mkdir()
    _pkg_root(config_dir, "mcps-pkg")
    cfg = _entry(args=["run", "--directory", "mcps-pkg", "ppsspp-dfx-mcp"])
    assert check_env.validate_mcp_entry(config_dir, cfg) is None


def test_directory_pinned_absolute_form_validates_clean(check_env: Any, tmp_path: Path) -> None:
    config_dir = tmp_path / "ws"
    config_dir.mkdir()
    pkg = _pkg_root(tmp_path, "elsewhere")
    cfg = _entry(args=["run", "--directory", str(pkg), "ppsspp-dfx-mcp"])
    assert check_env.validate_mcp_entry(config_dir, cfg) is None


# ── the L4 regression: platform-bound interpreter paths ───────────────────
@pytest.mark.parametrize(
    "command",
    [
        ".venv/ppsspp-dfx-mcp/Scripts/python.exe",  # the original W11 form
        ".venv/ppsspp-dfx-mcp/bin/python",  # its POSIX twin
        r"C:\some\where\.venv\Scripts\python.exe",
        "/usr/local/.venv/bin/python",
    ],
)
def test_platform_bound_command_is_the_l4_regression(
    check_env: Any, tmp_path: Path, command: str
) -> None:
    """specs/010 FR-008: a platform-bound interpreter path MUST be rejected."""
    config_dir = _pkg_root(tmp_path)
    issue = check_env.validate_mcp_entry(
        config_dir, _entry(command=command, args=["-m", "ppsspp_dfx_mcp"])
    )
    assert issue is not None, f"平台绑定路径必须被拒绝：{command!r}"
    assert "平台绑定" in issue, f"错误信息未点明原因：{issue!r}"
    assert "uv" in issue, f"错误信息未给出修复形式：{issue!r}"


def test_non_runner_command_is_rejected(check_env: Any, tmp_path: Path) -> None:
    config_dir = _pkg_root(tmp_path)
    issue = check_env.validate_mcp_entry(config_dir, _entry(command="python"))
    assert issue is not None
    assert "通用运行器" in issue


def test_missing_runner_on_path_is_reported(
    check_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-010: the runner itself is the execution target now -- treat it as a prerequisite."""
    monkeypatch.setattr(check_env, "runner_available", lambda: None)
    config_dir = _pkg_root(tmp_path)
    issue = check_env.validate_mcp_entry(config_dir, _entry())
    assert issue is not None
    assert "PATH" in issue, f"未点明运行器不可解析：{issue!r}"


def test_real_runner_lookup_consults_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """**不 mock 该函数本身**，而是 mock 它依赖的 PATH 查询。

    为什么必须这样：若像上面那条一样把 `runner_available` 整个替换掉，那么
    "它是否真的查 PATH"这件事就永远测不到——把函数体改成 `return "x"` 也不会有任何
    用例变红（变异检验 M-9 实测：判据空转）。本条锁住真实实现。
    """
    mod = _load_check_env()  # 刻意不用 fixture：fixture 会替换掉被测函数
    seen: list[str] = []

    def fake_which(name: str) -> str | None:
        seen.append(name)
        return "/fake/path/uv" if name == mod.GENERAL_RUNNER else None

    monkeypatch.setattr(mod.shutil, "which", fake_which)
    assert mod.runner_available() == "/fake/path/uv"
    assert seen == [mod.GENERAL_RUNNER], f"未按名查询 PATH：{seen}"

    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    assert mod.runner_available() is None, "PATH 上没有运行器时必须返回 None（而非乐观默认）"


# ── args ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad",
    [
        [],
        ["ppsspp-dfx-mcp"],
        ["-m", "ppsspp_dfx_mcp"],  # the retired form
        ["run"],  # no entry name
        ["run", "some-other-entry"],
        ["run", "--directory", "pkg"],  # missing entry name
        ["run", "--directory", "pkg", "other"],  # wrong entry name
        ["--directory", "pkg", "run", "ppsspp-dfx-mcp"],
    ],
)
def test_wrong_args_are_rejected(check_env: Any, tmp_path: Path, bad: list[str]) -> None:
    config_dir = _pkg_root(tmp_path)
    issue = check_env.validate_mcp_entry(config_dir, _entry(args=bad))
    assert issue is not None, f"args={bad!r} 应被拒绝"
    assert "args 不合法" in issue


def test_directory_pinned_args_require_existing_package_root(
    check_env: Any, tmp_path: Path
) -> None:
    """FR-010: the pinned target must exist AND look like a package root."""
    config_dir = tmp_path / "ws"
    config_dir.mkdir()

    missing = _entry(args=["run", "--directory", "nope", "ppsspp-dfx-mcp"])
    issue = check_env.validate_mcp_entry(config_dir, missing)
    assert issue is not None and "不是目录" in issue, f"缺失目录应被拒绝：{issue!r}"

    bare = config_dir / "bare"
    bare.mkdir()  # exists, but no pyproject.toml
    no_pyproject = _entry(args=["run", "--directory", "bare", "ppsspp-dfx-mcp"])
    issue = check_env.validate_mcp_entry(config_dir, no_pyproject)
    assert issue is not None and "pyproject.toml" in issue, f"不像包根应被拒绝：{issue!r}"


# ── env / structure ───────────────────────────────────────────────────────
def test_debug_log_default_is_rejected(check_env: Any, tmp_path: Path) -> None:
    """FR-009: no debug-level log default may be injected by the committed config."""
    config_dir = _pkg_root(tmp_path)
    issue = check_env.validate_mcp_entry(config_dir, _entry(env={"PPSSPP_DFX_LOG_LEVEL": "DEBUG"}))
    assert issue is not None
    assert "DEBUG" in issue


def test_non_debug_env_is_allowed(check_env: Any, tmp_path: Path) -> None:
    config_dir = _pkg_root(tmp_path)
    assert (
        check_env.validate_mcp_entry(config_dir, _entry(env={"PPSSPP_DFX_LOG_LEVEL": "WARNING"}))
        is None
    )


def test_workspacefolder_variable_fails(check_env: Any, tmp_path: Path) -> None:
    config_dir = _pkg_root(tmp_path)
    cfg = _entry(command="${workspaceFolder}/.venv/ppsspp-dfx-mcp/Scripts/python.exe")
    issue = check_env.validate_mcp_entry(config_dir, cfg)
    assert issue is not None
    assert "workspaceFolder" in issue


def test_missing_entry_fails(check_env: Any, tmp_path: Path) -> None:
    issue = check_env.validate_mcp_entry(tmp_path, {"mcpServers": {}})
    assert issue is not None and "ppsspp-dfx" in issue


def test_non_object_config_fails(check_env: Any, tmp_path: Path) -> None:
    assert check_env.validate_mcp_entry(tmp_path, []) is not None


def test_unparseable_config_reports_parse_error(check_env: Any, tmp_path: Path) -> None:
    config_dir = tmp_path / "pkg"
    config_dir.mkdir()
    (config_dir / ".mcp.json").write_text("{ not valid json", encoding="utf-8")
    issue = check_env.validate_mcp_config(config_dir)
    assert issue is not None


def test_mcp_config_dirs_dedups_and_lists_both(
    check_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws = tmp_path / "ws"
    pkg = tmp_path / "pkg"
    ws.mkdir()
    pkg.mkdir()
    (ws / ".mcp.json").write_text(json.dumps(_entry()), encoding="utf-8")
    (pkg / ".mcp.json").write_text(json.dumps(_entry()), encoding="utf-8")
    monkeypatch.setattr(check_env, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(check_env, "PACKAGE_ROOT", pkg)

    assert [d.resolve() for d in check_env.mcp_config_dirs()] == [ws.resolve(), pkg.resolve()]

    # Both roles resolving to the same .mcp.json → listed once.
    monkeypatch.setattr(check_env, "PACKAGE_ROOT", ws)
    assert [d.resolve() for d in check_env.mcp_config_dirs()] == [ws.resolve()]


# ── bootstrap wiring (per-config venv provisioning is RETIRED) ────────────
def test_bootstrap_provisions_main_venv_only(
    check_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """specs/010 T025: the runner self-provisions, so only the MAIN venv is ensured.

    This replaces the old structure lock, which required bootstrap to create a
    venv beside every committed `.mcp.json`. That requirement belonged to the
    retired venv-interpreter contract.
    """
    ws = tmp_path / "ws"
    pkg = tmp_path / "pkg"
    ws.mkdir()
    pkg.mkdir()
    (ws / ".mcp.json").write_text(json.dumps(_entry()), encoding="utf-8")
    (pkg / ".mcp.json").write_text(json.dumps(_entry()), encoding="utf-8")
    main_venv = tmp_path / "mainvenv"

    monkeypatch.setattr(check_env, "WORKSPACE_ROOT", ws)
    monkeypatch.setattr(check_env, "PACKAGE_ROOT", pkg)
    monkeypatch.setattr(check_env, "VENV_DIR", main_venv)
    monkeypatch.setattr(check_env, "check", lambda: 0)

    calls: list[Path] = []
    monkeypatch.setattr(check_env, "ensure_venv", lambda target: calls.append(target) or None)

    assert check_env.bootstrap() == 0
    assert main_venv in calls, "未为主 venv 调用 ensure_venv"
    assert calls == [main_venv], f"运行器形式下不应再为配置旁补 venv：calls={calls}"


# ── end-to-end: the configs actually committed in this checkout ───────────
def test_every_committed_mcp_json_is_launchable(check_env: Any) -> None:
    """Guard the real files, not only synthetic ones.

    Skips a config that is absent — the release checkout has only the package
    one, the monorepo has both.
    """
    checked = 0
    for config_dir in check_env.mcp_config_dirs():
        issue = check_env.validate_mcp_config(config_dir)
        assert issue is None, f"{config_dir / '.mcp.json'} 不合规：{issue}"
        checked += 1
    assert checked >= 1, "至少应有一份提交的 .mcp.json 被校验"
