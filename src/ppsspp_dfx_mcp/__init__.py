"""ppsspp-dfx-mcp: PPSSPP debug MCP server — engineering-grade debugging SDK."""

from importlib.metadata import PackageNotFoundError, version

try:
    # pyproject is the single source of truth; a hardcoded copy went stale
    # in 0.1.1 (handshake serverInfo self-reported 0.1.0).
    __version__ = version("ppsspp-dfx-mcp")
except PackageNotFoundError:  # source-tree import, package not installed
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
