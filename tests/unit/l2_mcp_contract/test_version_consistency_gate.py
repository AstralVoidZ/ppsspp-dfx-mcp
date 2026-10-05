"""G-5 (FR-005): the handshake version must match pyproject.

Deep-test finding (mcp_test_report/summary.md §5, P-VERSION; also noted in
every per-tool report's header): the handshake self-reported 0.1.6 while
pyproject declared 0.1.7.

Root cause is NOT the version logic. `__init__.py` correctly derives
`__version__` from `importlib.metadata.version("ppsspp-dfx-mcp")` (a
hardcoded copy went stale in 0.1.1), and `registry.py` feeds it straight
into `MCPServer(version=...)`. The drift came from a STALE EDITABLE INSTALL:
the dist-info metadata `importlib.metadata` reads still said 0.1.6 after
pyproject moved to 0.1.7, so a correct implementation reported a wrong
number. No amount of editing `__init__.py` would have fixed that — the fix is
resyncing the install and adding a gate so the next bump cannot drift
silently again.

The chain this test pins, end to end:

    pyproject [project] version
      -> importlib.metadata.version("ppsspp-dfx-mcp")   (what __init__ reads)
      -> ppsspp_dfx_mcp.__version__
      -> MCPServer(version=...)                          (what the client sees)

Every assertion here is falsifiable: bump either side (pyproject or the
installed dist-info) without the other and the gate goes red. That is the
whole point — G-5 recurred because nothing failed when the two diverged.
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

from ppsspp_dfx_mcp import __version__

PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


def _pyproject_version() -> str:
    """The declared version — the single source of truth per __init__.py."""
    assert PYPROJECT.is_file(), f"pyproject missing at {PYPROJECT}"
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


class TestVersionChainIsConsistent:
    def test_declared_version_parses(self) -> None:
        """Guards the reader itself: a malformed pyproject would make every
        comparison below vacuously pass or error confusingly."""
        declared = _pyproject_version()
        assert declared, "pyproject [project] version is empty"
        assert declared.count(".") == 2, f"unexpected version shape: {declared!r}"

    def test_runtime_version_matches_pyproject(self) -> None:
        """THE G-5 ASSERTION: __version__ == pyproject version.

        Fails when pyproject is bumped without resyncing the install (the
        original defect), and when the metadata is ahead of the source.
        """
        declared = _pyproject_version()
        assert __version__ == declared, (
            f"version drift: package reports {__version__!r} but "
            f"pyproject declares {declared!r}. The reported value comes from "
            f"the installed distribution metadata, so this means the editable "
            f"install is stale — resync it (`pip install -e .`) rather than "
            f"editing __init__.py (G-5 / FR-005)."
        )

    def test_installed_metadata_matches_pyproject(self) -> None:
        """Pin the layer that actually drifted.

        `__version__` is read FROM this metadata, so asserting the two
        together is what caught the original 0.1.6-vs-0.1.7 split. Kept as
        a separate case so the failure message names the stale layer.
        """
        try:
            installed = version("ppsspp-dfx-mcp")
        except PackageNotFoundError:  # pragma: no cover - source-tree import
            pytest.skip("package not installed; the metadata layer does not exist here")
        declared = _pyproject_version()
        assert installed == declared, (
            f"installed distribution metadata says {installed!r} but "
            f"pyproject declares {declared!r} — the editable install is stale "
            f"(G-5); run `pip install -e .`"
        )

    def test_version_is_not_the_unknown_fallback(self) -> None:
        """A source-tree import yields '0.0.0+unknown'; that must not be
        allowed to pose as a real handshake version."""
        assert __version__ != "0.0.0+unknown", (
            "package reports the not-installed fallback — the handshake would "
            "advertise 0.0.0+unknown to every client (G-5)"
        )


class TestHandshakeCarriesTheVersion:
    def test_server_reports_the_same_version(self) -> None:
        """The value a client sees at handshake must be the pinned one.

        Ties the assertion to the real surface: `MCPServer(version=...)` is
        what lands in `serverInfo.version`, so a future edit that decouples
        the constructor from `__version__` fails here.
        """
        from ppsspp_dfx_mcp.registry import mcp

        assert getattr(mcp, "version", None) == _pyproject_version(), (
            "the MCP server's advertised version has drifted from pyproject"
        )
