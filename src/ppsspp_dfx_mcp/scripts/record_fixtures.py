"""Record fixture responses from a live PPSSPP WebSocket debugger.

This module was referenced across the codebase (config.py, client_helper.py,
integration conftest error hints) but was never implemented — this script
fills that gap so fixtures can be regenerated against a real PPSSPP.

Usage (from mcps/ppsspp-dfx-mcp, with src/ importable):

    python -m ppsspp_dfx_mcp.scripts.record_fixtures \
        [--host 127.0.0.1] [--port 12345] \
        [--out tests/cassettes/fixtures] \
        [--extend]

Prerequisites: PPSSPP running with Settings → Tools → "Enable remote
debugger" (WebSocket debugger on port 12345, subprotocol
debugger.ppsspp.org) and a game loaded. The script connects directly —
it does NOT spawn PPSSPP.

Default recipe records the read-only, side-effect-free events. With
``--extend`` a second block of game-dependent events is recorded too
(they assume the TOP main module at 0x08804000, matching
.ppsspp-dfx/config/addresses.yaml).

Output format per event (consumed by tests/contract_recorder/fixture_loader.py):

    {
      "ppsspp_version": "<PPSSPP_GIT_VERSION from the version event>",
      "type": "call",
      "records": [{"params": {...}, "response": {...}}]
    }
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("record_fixtures")

# (event, params, note) — read-only, no side effects, safe on any game.
_SAFE_RECIPE: list[tuple[str, dict[str, Any], str]] = [
    ("cpu.status", {}, "stepping/paused/pc/ticks"),
    ("cpu.getAllRegs", {}, "categories parallel arrays"),
    ("game.status", {}, "game dict + paused"),
    ("hle.thread.list", {}, "threads array"),
    ("hle.module.list", {}, "modules array"),
    ("hle.func.list", {}, "functions array"),
    ("memory.mapping", {}, "ranges array"),
    ("memory.base", {}, "addressHex"),
]

# Game-dependent extras (--extend): assume TOP main module at 0x08804000.
_TOP_BASE = 0x08804000
_EXTEND_RECIPE: list[tuple[str, dict[str, Any], str]] = [
    ("memory.read_u32", {"address": _TOP_BASE}, "ELF magic read"),
    ("memory.disasm", {"address": _TOP_BASE, "count": 8}, "disasm lines"),
    ("memory.searchDisasm", {"address": _TOP_BASE, "match": "jr"}, "first match"),
]


def _write_fixture(
    out_dir: Path,
    event: str,
    version: str,
    params: dict[str, Any],
    response: dict[str, Any],
) -> Path:
    payload = {
        "ppsspp_version": version,
        "type": "call",
        "records": [{"params": params, "response": response}],
    }
    # Event names contain dots (cpu.getAllRegs) — keep them in the stem so
    # fixture_loader's path.stem → event round-trip stays exact.
    path = out_dir / f"{event}.json"
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


async def record(host: str, port: int, out_dir: Path, extend: bool) -> int:
    from ppsspp_dfx_mcp.core.transport import WsTransport

    transport = WsTransport(host, port)
    await transport.connect()
    try:
        # The version event doubles as the subprotocol handshake probe
        # (WebSocket.cpp:43 recommends sending it right after connect).
        version_resp = await transport.call(
            "version",
            timeout=2.0,
            name="ppsspp-dfx-record",
            version="1.0.0",
        )
        version = str(version_resp.get("version", "unknown"))

        recipe = list(_SAFE_RECIPE)
        if extend:
            recipe += _EXTEND_RECIPE

        written = 0
        for event, params, _note in recipe:
            try:
                resp = await transport.call(event, timeout=10.0, **params)
            except Exception as e:  # noqa: BLE001 — record per-event failures
                logger.warning("skip %s: %s", event, e)
                continue
            path = _write_fixture(out_dir, event, version, params, resp)
            logger.info("recorded %s → %s", event, path)
            written += 1
        return written
    finally:
        with contextlib.suppress(Exception):  # best-effort close
            await transport.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ppsspp_dfx_mcp.scripts.record_fixtures",
        description="Record fixture responses from a live PPSSPP debugger.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=12345)
    parser.add_argument(
        "--out",
        default=None,
        help=(
            "output directory (default: <cwd>/tests/cassettes/fixtures — "
            "cwd-relative so a pip-installed copy never writes "
            "into site-packages)"
        ),
    )
    parser.add_argument(
        "--extend",
        action="store_true",
        help="also record game-dependent events (TOP module at 0x08804000)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    out_arg = args.out
    if out_arg is None:
        # cwd-relative default — resolving off the installed file
        # would write into site-packages.
        out_arg = str(Path.cwd() / "tests" / "cassettes" / "fixtures")
    out_dir = Path(out_arg)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = asyncio.run(record(args.host, args.port, out_dir, args.extend))
    logger.info("done: %d fixtures written to %s", written, out_dir)
    return 0 if written else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
