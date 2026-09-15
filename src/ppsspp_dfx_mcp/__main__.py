"""Entry point: python -m ppsspp_dfx_mcp [--log-level <level>] [--rate-limit <int>].

CLI options take precedence over environment variables (C-route: CLI is
a thin wrapper that sets env vars then delegates to server.main()).
"""

from __future__ import annotations

import argparse
import os
import sys

from ppsspp_dfx_mcp.server import main


def run() -> None:
    parser = argparse.ArgumentParser(
        prog="ppsspp-dfx-mcp",
        description="PPSSPP debug MCP server — engineering-grade debugging SDK.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("PPSSPP_DFX_LOG_LEVEL", "INFO"),
        help="Log level (overrides PPSSPP_DFX_LOG_LEVEL env var; default: INFO).",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=int(os.environ.get("PPSSPP_DFX_RATE_LIMIT", "60")),
        help="Per-tool rate limit in calls-per-minute (0 disables; default: 60).",
    )
    parser.add_argument(
        "--ppsspp-exe",
        default=os.environ.get("PPSSPP_DFX_EXE_PATH", ""),
        help="Path to PPSSPP executable (overrides PPSSPP_DFX_EXE_PATH env var).",
    )
    parser.add_argument(
        "--ws-host",
        default=os.environ.get("PPSSPP_DFX_WS_HOST", "127.0.0.1"),
        help="PPSSPP WebSocket host (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--ws-port",
        type=int,
        default=int(os.environ.get("PPSSPP_DFX_WS_PORT", "12345")),
        help="PPSSPP WebSocket port (default: 12345).",
    )
    # Accept but ignore unknown args so FastMCP can process its own flags.
    args, _ = parser.parse_known_args(sys.argv[1:])

    if args.log_level:
        os.environ["PPSSPP_DFX_LOG_LEVEL"] = args.log_level
    if args.rate_limit is not None:
        os.environ["PPSSPP_DFX_RATE_LIMIT"] = str(args.rate_limit)
    if args.ppsspp_exe:
        os.environ["PPSSPP_DFX_EXE_PATH"] = args.ppsspp_exe
    if args.ws_host:
        os.environ["PPSSPP_DFX_WS_HOST"] = args.ws_host
    if args.ws_port:
        os.environ["PPSSPP_DFX_WS_PORT"] = str(args.ws_port)

    # Logging is configured by server.main() after env vars are set, so no
    # redundant configure_logging() call here (was previously double-invoked).
    main()


if __name__ == "__main__":
    run()
