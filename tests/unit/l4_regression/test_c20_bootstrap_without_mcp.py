"""C-20 lock: the environment bootstrap must run before `mcp` exists.

`scripts/check_env.py` is the thing that CREATES the venv which installs `mcp`
(`--bootstrap`). It imports `scripts/_wire.py` for PACKAGE_ROOT/WORKSPACE_ROOT,
and that module used to do `from mcp import StdioServerParameters` at module
level — so on a clean clone the bootstrap died with

    ModuleNotFoundError: No module named 'mcp'

before it could install anything. The fix makes those two imports lazy inside
the functions that need them; this test pins it by blocking `mcp` in
`sys.modules` and running the real `--help`, which works in any environment
(with or without the dependency installed) because the block is explicit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_PKG = Path(__file__).resolve().parents[3]
_CHECK_ENV = _PKG / "scripts" / "check_env.py"

#: Every `mcp` module `scripts/` imports at top level somewhere. `None` in
#: sys.modules makes the import raise ImportError deterministically.
_BLOCKED = ("mcp", "mcp.client", "mcp.client.stdio", "mcp.types", "mcp.server")


def _run_without_mcp(*argv: str) -> subprocess.CompletedProcess[str]:
    code = (
        "import runpy, sys\n"
        f"sys.path.insert(0, {str(_CHECK_ENV.parent)!r})\n"
        f"for name in {_BLOCKED!r}:\n"
        "    sys.modules[name] = None\n"
        f"sys.argv = ['check_env.py', *{list(argv)!r}]\n"
        f"runpy.run_path({str(_CHECK_ENV)!r}, run_name='__main__')\n"
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_help_runs_without_mcp() -> None:
    """The bootstrap's own help text must not require the package it installs."""
    proc = _run_without_mcp("--help")
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "usage:" in proc.stdout
    assert "--bootstrap" in proc.stdout


def test_print_config_runs_without_mcp() -> None:
    """`--print-config` is pure path/config rendering: no SDK needed."""
    proc = _run_without_mcp("--print-config")
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "mcpServers" in proc.stdout


def test_wire_reports_the_missing_dependency_only_when_used() -> None:
    """Lazy means lazy: importing `_wire` is fine, building params is not.

    Without this, a regression that re-adds the top-level import would still
    pass the two tests above if `check_env.py` ever stopped importing `_wire`.
    """
    code = (
        "import sys\n"
        f"sys.path.insert(0, {str(_CHECK_ENV.parent)!r})\n"
        f"for name in {_BLOCKED!r}:\n"
        "    sys.modules[name] = None\n"
        "import _wire\n"
        "print('import ok')\n"
        "try:\n"
        "    _wire.build_stdio_params()\n"
        "except ImportError:\n"
        "    print('use raises ImportError')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert "import ok" in proc.stdout
    assert "use raises ImportError" in proc.stdout
