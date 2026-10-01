"""Run the CI gate locally, in CI order, and report every step that failed.

`pytest tests -q` is not the gate: CI runs six steps, and running them one at a
time teaches you one thing per commit — the first failure hides the rest. That
is how a branch reached CI carrying a SyntaxError, an unformatted file and an
unparseable contract baseline while each local run had looked green.

Every step always runs; the exit code is 0 only when all of them pass.

Usage (from the repository root):
    python scripts/check_gate.py [--ruff PATH]
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def _ruff_executable(override: str | None) -> str | None:
    """The ruff CI would use: `--ruff`, else the interpreter's own, else PATH."""
    if override:
        return override
    sibling = Path(sys.executable).with_name("ruff.exe" if os.name == "nt" else "ruff")
    if sibling.is_file():
        return str(sibling)
    return shutil.which("ruff")


def _pinned_ruff() -> str | None:
    """The pin CI installs, read from pyproject's dev groups."""
    data = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    groups = (
        data.get("project", {}).get("optional-dependencies", {}).get("dev", []),
        data.get("dependency-groups", {}).get("dev", []),
    )
    for entries in groups:
        for entry in entries:
            match = re.fullmatch(r"ruff==([\w.]+)", entry)
            if match:
                return match.group(1)
    return None


def _ruff_version(ruff: str) -> str | None:
    try:
        completed = subprocess.run([ruff, "--version"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return completed.stdout.strip().split()[-1] or None


def _ruff_check(ruff: str | None, pinned: str | None) -> tuple[bool, str]:
    """A drifted ruff makes this run's verdict a different verdict than CI's."""
    if ruff is None:
        return False, (
            "ruff not found next to the interpreter or on PATH — "
            "install the dev extras: python -m pip install -e .[dev]"
        )
    version = _ruff_version(ruff)
    if version is None:
        return False, f"cannot run `{ruff} --version`"
    if pinned and version != pinned:
        return False, (
            f"ruff {version} != pinned {pinned}: `ruff format` output is "
            f"version-sensitive, so a run with a drifted ruff is not the CI "
            f"verdict — python -m pip install -e .[dev] (or pass --ruff PATH)"
        )
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ruff", help="path to the ruff executable to use")
    args = parser.parse_args()

    ruff = _ruff_executable(args.ruff)
    pinned = _pinned_ruff()
    ruff_ok, ruff_note = _ruff_check(ruff, pinned)
    if not ruff_ok:
        print(f"!! {ruff_note}\n")

    steps: list[tuple[str, list[str]]] = [
        ("precheck (syntax + JSON)", [sys.executable, "scripts/check_syntax.py"]),
        ("ruff check", [ruff or "ruff", "check", "."]),
        ("ruff format --check", [ruff or "ruff", "format", "--check", "."]),
        ("pytest tests", [sys.executable, "-m", "pytest", "tests", "-q"]),
        ("skip audit", [sys.executable, "scripts/check_skips.py"]),
        ("pytest evals", [sys.executable, "-m", "pytest", "evals", "-q"]),
    ]

    results: list[tuple[str, bool, float]] = [(f"ruff version == pinned {pinned}", ruff_ok, 0.0)]
    for label, command in steps:
        print(f"===== {label} =====", flush=True)
        started = time.monotonic()
        completed = subprocess.run(command, cwd=_REPO)
        results.append((label, completed.returncode == 0, time.monotonic() - started))

    print("\n===== gate summary =====")
    for label, ok, seconds in results:
        print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({seconds:.1f}s)" if seconds else ""))
    failed = [label for label, ok, _ in results if not ok]
    if failed:
        print(f"\nGATE FAILED — {len(failed)}/{len(results)} steps: {', '.join(failed)}")
        return 1
    print(f"\nGATE PASSED — {len(results)} steps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
