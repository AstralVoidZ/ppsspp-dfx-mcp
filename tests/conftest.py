"""Package-level pytest conftest: path injection + session isolation.

Two responsibilities:
1. Inject `tests/` root into sys.path so `from fake_transport import FakeTransport`
   works in L1/L3/L4 contract tests.
2. Session isolation fixtures (autouse): redirect sessions.json to tmp_path
   so tests never touch the real ~/.ppsspp-dfx/sessions.json.

Note (SDK v2): the pre-v2 PyWin32 stub injection was removed — the SDK's
Windows stdio transport genuinely calls win32job/win32api (`mcp.os.win32`),
and `pywin32` is a conditional hard dependency of `mcp>=2` on win32, so
stubbing it would only mask a missing dependency with a broken runtime.

Design note: `pythonpath = ["src"]` in pyproject.toml handles
`ppsspp_dfx_mcp` package discovery — no manual sys.path injection needed
for the package itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# ---------- R13: run-directory guard (fail fast with the right command) --

# The suite MUST run from the package root (the repository root):
# running it from the repo root mixes the root and mcps pytest configs and
# produces order-dependent false failures (verification round 3, G4 — a
# git-stash control experiment proved the failures are environmental).
_PKG_ROOT = Path(__file__).resolve().parent.parent
if Path.cwd().resolve() != _PKG_ROOT:
    raise SystemExit(
        "\n[ppsspp-dfx-mcp] mcps tests must run from the package root:\n"
        "    cd <repo root> && python -m pytest tests\n"
        f"  current cwd: {Path.cwd()}\n"
        "  (R13 guard: cross-config runs produce unreliable results — see "
        "design_ppsspp_dfx_mcp_test_refactor_v2.md §G4)"
    )

# ---------- tests/ root path injection (for fake_transport) ----------

_TESTS_ROOT = Path(__file__).resolve().parent
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))


# ---------- Session isolation fixtures ----------


@pytest.fixture(autouse=True)
def isolated_sessions_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect sessions.json to tmp_path so tests never touch the real file.

    The real ~/.ppsspp-dfx/sessions.json must not be created or modified by
    the test suite. We achieve isolation three ways for robustness:
      1. Set PPSSPP_DFX_SESSIONS_PATH env var (read by config.sessions_path()).
      2. Monkeypatch config.sessions_path to return the tmp path directly.
      3. Monkeypatch session.session_manager.sessions_path (it imported
         the function by reference, so the env var alone wouldn't catch the
         bound reference — but env var DOES catch it because the original
         function reads os.environ at call time; the monkeypatch is defense
         in depth).
    """
    target = tmp_path / "sessions.json"

    monkeypatch.setenv("PPSSPP_DFX_SESSIONS_PATH", str(target))

    def _fake_sessions_path() -> Path:
        return target

    monkeypatch.setattr("ppsspp_dfx_mcp.config.sessions_path", _fake_sessions_path)
    monkeypatch.setattr(
        "ppsspp_dfx_mcp.session.session_manager.sessions_path",
        _fake_sessions_path,
    )
    return target


@pytest.fixture
def isolated_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point PPSSPP_DFX_CONFIG_DIR at a tmp dir so YAML reads/writes are isolated.

    Opt-in (not autouse): tests that exercise config_dir() env-var behavior
    need to control the env var directly and must not request this fixture.
    """
    cfg = tmp_path / "config"
    cfg.mkdir()
    monkeypatch.setenv("PPSSPP_DFX_CONFIG_DIR", str(cfg))
    return cfg
