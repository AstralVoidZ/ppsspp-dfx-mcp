"""Parse every committed Python and JSON file — no imports, no test collection.

A file that cannot be parsed hides everything else wrong inside it: `ruff check`
reports only `invalid-syntax` for that file and silently skips its other rules,
and a broken JSON data file surfaces as a `json.decoder.JSONDecodeError` in
whichever unrelated test module happens to load it first. Both take milliseconds
to detect, so they are detected here — before lint and before pytest — with a
message that names the file and the line.

Usage (from the repository root):
    python scripts/check_syntax.py

Exit code 0 when every file parses, 1 otherwise (one line per failure).
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

# Generated files get a pointer to their generator instead of a bare parse
# error: hand-editing a generated file is the failure mode this catches.
_JSON_HINTS = {
    "tool_surface_baseline.json": (
        "generated — regenerate with `python scripts/dump_tool_surface.py`; "
        "never hand-edit or hand-merge it"
    ),
}


def _committed_files() -> list[Path]:
    """Tracked files, plus new files git does not ignore."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=_REPO,
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"note: `git ls-files` unavailable ({exc}) — walking the tree instead")
        skip = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "node_modules"}
        return [
            path
            for path in sorted(_REPO.rglob("*"))
            if path.suffix in {".py", ".json"} and path.is_file() and not (skip & set(path.parts))
        ]
    return [Path(name) for name in out.decode("utf-8").split("\0") if name]


def _check_python(rel: Path) -> str | None:
    try:
        ast.parse((_REPO / rel).read_text(encoding="utf-8"), filename=str(rel))
    except SyntaxError as exc:
        return f"{rel}:{exc.lineno}:{exc.offset}: {exc.msg}"
    except UnicodeDecodeError as exc:
        return f"{rel}: not valid UTF-8 ({exc})"
    return None


def _check_json(rel: Path) -> str | None:
    try:
        json.loads((_REPO / rel).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        hint = _JSON_HINTS.get(rel.name)
        return f"{rel}:{exc.lineno}:{exc.colno}: invalid JSON — {exc.msg}" + (
            f" ({hint})" if hint else ""
        )
    except UnicodeDecodeError as exc:
        return f"{rel}: not valid UTF-8 ({exc})"
    return None


def main() -> int:
    python_files = json_files = 0
    failures: list[str] = []

    for rel in sorted(_committed_files()):
        if rel.suffix not in {".py", ".json"} or not (_REPO / rel).is_file():
            continue
        if rel.suffix == ".py":
            python_files += 1
            failure = _check_python(rel)
        else:
            json_files += 1
            failure = _check_json(rel)
        if failure:
            failures.append(failure)

    for failure in failures:
        print(failure)
    print(
        f"parsed {python_files + json_files} files "
        f"({python_files} .py, {json_files} .json) — {len(failures)} unparseable"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
