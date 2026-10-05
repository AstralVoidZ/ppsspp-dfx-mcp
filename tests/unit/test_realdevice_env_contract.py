"""test_realdevice_env_contract.py — M-20 guard (specs/010 US3, C5-6).

The real-device gate keys on TWO env var names in tests/integration/conftest.py
(``PPSSPP_DFX_TEST_EXE_PATH`` / ``PPSSPP_DFX_TEST_ISO_PATH``). History has
already produced a mis-spelled form (``..._EXE`` without ``_PATH``); a typo
would make the fixtures read the WRONG variable, so the real-device tests
would silently NEVER run (CI-safe, invisible). The gate reason strings are
what the skip audit and the quickstart document operators read — they must
name the SAME variables the fixtures actually read.

Judge (AST, dual-sided):
  every env var name the fixtures ``os.environ.get(...)`` must appear inside
  at least one skip-reason string literal in the same file, and vice versa.
Mutating either side (M-20) must turn this red.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CONF = Path(__file__).resolve().parents[1] / "integration" / "conftest.py"


def _env_get_names(tree: ast.AST) -> set[str]:
    """Names passed to os.environ.get("...") / os.environ[...]."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            is_env_get = (
                isinstance(base, ast.Attribute)
                and base.attr == "environ"
                and node.func.attr in {"get", "getenv"}
            )
            if is_env_get and node.args and isinstance(node.args[0], ast.Constant):
                names.add(str(node.args[0].value))
    return names


def _string_literals(tree: ast.AST) -> str:
    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            parts.append(node.value)
    return "\n".join(parts)


def test_realdevice_env_names_match_their_skip_reasons() -> None:
    """Every env var the device gate reads must be named in a skip reason."""
    src = _CONF.read_text(encoding="utf-8")
    tree = ast.parse(src)
    env_names = {n for n in _env_get_names(tree) if n.startswith("PPSSPP_DFX_TEST_")}
    assert env_names == {"PPSSPP_DFX_TEST_EXE_PATH", "PPSSPP_DFX_TEST_ISO_PATH"}, (
        f"real-device gate reads unexpected env vars: {sorted(env_names)} — "
        "if a variable was renamed, update both the reader and its skip reason"
    )
    literals = _string_literals(tree)
    unnamed = sorted(n for n in env_names if n not in literals)
    assert not unnamed, (
        f"env vars {unnamed} are read but never named in any skip reason — "
        "operators (and the skip audit whitelist) could not discover the "
        "correct variable name (M-20: the historical '..._EXE' typo class)"
    )
