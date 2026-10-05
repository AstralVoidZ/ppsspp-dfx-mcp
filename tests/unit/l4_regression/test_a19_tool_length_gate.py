"""A19 gate: tool-function length limit with frozen exemptions.

Runs `scripts/check_tool_function_length.py` against the REAL tree (must exit
0: every over-limit `@mcp.tool` function carries a `LONG-TOOL:` justification),
and proves the gate is falsifiable with a synthetic 121-line tool function in a
tmp directory. The justification lives in a COMMENT above the decorator, never
in the docstring (a tool's docstring is its wire description).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "check_tool_function_length.py"
TOOLS_DIR = ROOT / "src" / "ppsspp_dfx_mcp" / "tools"


def _run(tools_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(tools_dir)],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )


def _write_tool_module(path: Path, *, marker: str | None, in_docstring: bool = False) -> None:
    """Write a module whose single `@mcp.tool` function has a 123-line body.

    `marker` is placed as a comment directly above the decorator, unless
    `in_docstring` is True (then it goes into the docstring instead — used to
    prove the gate does NOT accept the docstring location).
    """
    doc = "PURPOSE: synthetic probe."
    comment = ""
    if marker is not None:
        if in_docstring:
            doc += f"\n\n    {marker}"
        else:
            comment = f"# {marker}\n"
    body = "\n".join(f"    x = x + {i}" for i in range(121))
    path.write_text(
        f"{comment}"
        "@mcp.tool()\n"
        "async def synthetic_tool():\n"
        f'    """{doc}"""\n'
        "    x = 0\n"
        f"{body}\n"
        "    return x\n",
        encoding="utf-8",
    )


def test_real_tree_passes_gate() -> None:
    """Every over-limit tool function in the real tree is justified."""
    assert SCRIPT.is_file(), f"gate script missing: {SCRIPT}"
    result = _run(TOOLS_DIR)
    assert result.returncode == 0, (
        "A19 length gate failed on the real tree — an over-limit @mcp.tool "
        f"function lacks a LONG-TOOL justification comment:\n{result.stderr}"
    )


def test_gate_fails_on_unjustified_121_line_tool(tmp_path: Path) -> None:
    """Falsifiability: a 123-line body with no marker must exit 1."""
    _write_tool_module(tmp_path / "bad_tool.py", marker=None)
    result = _run(tmp_path)
    assert result.returncode == 1, (
        "gate did NOT fail on an unjustified 121-line tool body — the gate is "
        f"vacuous. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "LONG-TOOL:" in result.stderr
    assert "synthetic_tool" in result.stderr


def test_gate_passes_when_comment_is_justified(tmp_path: Path) -> None:
    """The same long body passes once a LONG-TOOL comment justifies it
    (proves the failure above is caused by the missing marker, not the size)."""
    _write_tool_module(
        tmp_path / "ok_tool.py",
        marker="LONG-TOOL: kept long because the synthetic probe genuinely needs 121 steps.",
    )
    result = _run(tmp_path)
    assert result.returncode == 0, f"justified long function rejected:\n{result.stderr}"


def test_gate_rejects_marker_in_docstring(tmp_path: Path) -> None:
    """A docstring marker must NOT satisfy the gate (it would leak to the wire
    description). Only the comment above the decorator counts."""
    _write_tool_module(
        tmp_path / "doc_tool.py",
        marker="LONG-TOOL: this sits in the docstring and must be ignored.",
        in_docstring=True,
    )
    result = _run(tmp_path)
    assert result.returncode == 1, (
        "a docstring 'LONG-TOOL:' satisfied the gate — the annotation would "
        "leak into the model-visible tool description"
    )


def test_marker_requires_justification_text(tmp_path: Path) -> None:
    """A bare `LONG-TOOL:` with no justification sentence must still fail."""
    _write_tool_module(tmp_path / "bare_tool.py", marker="LONG-TOOL:")
    result = _run(tmp_path)
    assert result.returncode == 1, "a bare 'LONG-TOOL:' marker must not satisfy the gate"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
