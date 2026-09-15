"""Record real PPSSPP WebSocket communication to a cassette + fixtures.

This is the ONE-TIME recording step that produces the truth-source
artifacts used by L1 contract tests:

1. Launches PPSSPP via PpssppLauncher (random port + appendconfig)
2. Connects a real WsTransport
3. Wraps it in RecordingTransport (records all WS traffic to cassette.jsonl)
4. Also wraps it in ContractRecorder (records per-event fixtures/*.json)
5. Drives the 30-tool execution checklist from
   (Phase 1-5: health/session, core debug, script mgmt, P0 enhance, GPU)
6. flush() writes cassette.jsonl + fixtures/*.json
7. Cleanly shuts down PPSSPP

Usage:
    python -m ppsspp_dfx_mcp.scripts.record_fixtures \\
        --iso path/to/game.iso \\
        --output-dir tests/cassettes/

Outputs:
    tests/cassettes/real_ppsspp.jsonl       (cassette — sequential stream)
    tests/cassettes/fixtures/<event>.json    (fixtures — per-event slices)

Run this whenever PPSSPP behavior changes or new tools are added. The
produced artifacts are the single source of truth for L1 contract tests.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Optional

# Project root on sys.path so we can import ppsspp_dfx_mcp + record_replay.
# This file is at <package>/scripts/record_fixtures.py; the package root
# (pyproject.toml) is one level up — layout-independent.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
# tests first (for record_replay), then src (for ppsspp_dfx_mcp).
sys.path.insert(0, str(PACKAGE_ROOT / "tests"))
sys.path.insert(0, str(PACKAGE_ROOT / "src"))
sys.path.insert(0, str(PACKAGE_ROOT))

from ppsspp_dfx_mcp.config import ppsspp_exe_path  # noqa: E402
from ppsspp_dfx_mcp.core.launcher import PpssppLauncher  # noqa: E402
from ppsspp_dfx_mcp.core.transport import WsTransport  # noqa: E402
from record_replay.recording_transport import RecordingTransport  # noqa: E402
from contract_recorder import ContractRecorder  # noqa: E402

logger = logging.getLogger("record_fixtures")

# Default ISO path (relative to the working directory; override with --iso).
DEFAULT_ISO = "game.iso"
DEFAULT_OUTPUT_DIR = "tests/cassettes"


# ─── 30-tool execution checklist ────────────────────────────────────────
#
# Drives the 30-tool list from guide_ppsspp_dfx_mcp_live_test_methodology_v1.md
# §3.1-§3.5. Each entry: (description, event, params). Order matters —
# later tools depend on earlier state (e.g. breakpoint needs CPU paused).
#
# This is the SAME order used by the live test methodology guide, so the
# produced cassette faithfully captures real PPSSPP behavior across all
# 30 tools.

TOOL_CHECKLIST: list[tuple[str, str, dict[str, Any]]] = [
    # ─── Phase 2 — Core debug (WS-level events) ──────────────────────
    # Event names verified against open_source/ppsspp/Core/Debugger/WebSocket/
    # (BreakpointSubscriber.cpp / CPUCoreSubscriber.cpp / DisasmSubscriber.cpp /
    #  HLESubscriber.cpp / MemorySubscriber.cpp).
    #
    # 2.2 read memory (read_bytes via memory.read)
    ("2.2 read_bytes", "memory.read", {"address": 0x08804000, "size": 16}),
    # 2.3 read u32
    ("2.3 read_u32", "memory.read_u32", {"address": 0x08804000}),
    # 2.4 read string (SJIS game ID at 0x08808ADC)
    ("2.4 read_string", "memory.readString", {"address": 0x08808ADC}),
    # 2.8 disassemble
    ("2.8 disasm", "memory.disasm", {"address": 0x08828594, "count": 5}),
    # 2.9 get PC (cpu.status)
    ("2.9 cpu.status", "cpu.status", {}),
    # 2.10 pause (fire_and_forget, state change captured separately)
    ("2.10 pause", "cpu.stepping", {"step": "pause"}),
    # 2.11 step into
    ("2.11 step_into", "cpu.stepping", {"step": "into"}),
    # 2.16 backtrace (needs CPU paused) — event is hle.backtrace
    ("2.16 backtrace", "hle.backtrace", {"thread": 0}),
    # 2.12 resume
    ("2.12 resume", "cpu.stepping", {"step": "resume"}),
    # 2.17 func_add — event is hle.func.add
    ("2.17 func_add", "hle.func.add", {"address": 0x08828594, "name": "test_func"}),
    # 2.18 func_list — event is hle.func.list
    ("2.18 func_list", "hle.func.list", {}),
    # 2.19 func_remove — event is hle.func.remove
    ("2.19 func_remove", "hle.func.remove", {"address": 0x08828594}),
    # 2.13 breakpoint set
    ("2.13 bp_set", "cpu.breakpoint.add", {"address": 0x08828594, "type": "code"}),
    # 2.14 breakpoint list
    ("2.14 bp_list", "cpu.breakpoint.list", {}),
    # 2.15 breakpoint remove (PPSSPP requires 'address', not 'id')
    ("2.15 bp_remove", "cpu.breakpoint.remove", {"address": 0x08828594}),
    # ─── Extra: game.status + version (always-available events) ─────
    ("game.status", "game.status", {}),
    ("version", "version", {}),
    # ─── Extra: gpu.stats.get (CPU must be running) ─────────────────
    ("gpu.stats.get", "gpu.stats.get", {}),
    # ─── Extra: cpu.getAllRegs (needs CPU paused) ────────────────────
    # Pause first, then getAllRegs, then resume.
    ("pause_for_regs", "cpu.stepping", {"step": "pause"}),
    ("cpu.getAllRegs", "cpu.getAllRegs", {}),
    ("resume_after_regs", "cpu.stepping", {"step": "resume"}),
]


async def record_one(
    recorder: RecordingTransport,
    description: str,
    event: str,
    params: dict[str, Any],
) -> None:
    """Drive one tool from the checklist, recording the WS exchange.

    fire_and_forget events (cpu.stepping) use recorder.fire_and_forget;
    all other events use recorder.call.
    """
    logger.info("recording: %s (event=%s, params=%s)", description, event, params)
    try:
        if event == "cpu.stepping":
            await recorder.fire_and_forget(event, **params)
        else:
            await recorder.call(event, **params)
    except Exception as exc:
        # Don't abort the whole recording on one tool failure; log and continue.
        logger.warning("  tool '%s' failed: %s", description, exc)


async def run_recording(iso_path: Path, output_dir: Path) -> int:
    """Run the full recording session.

    Returns the number of records written to the cassette.
    """
    cassette_path = output_dir / "real_ppsspp.jsonl"
    fixtures_dir = output_dir / "fixtures"

    # Step 1: launch PPSSPP with the ISO.
    logger.info("launching PPSSPP (iso=%s)", iso_path)
    launcher = PpssppLauncher()
    await launcher.start(iso_path=iso_path, wait_seconds=8.0, windowed=True)
    ws_port = launcher.ws_port
    if ws_port is None:
        logger.error("PPSSPP failed to bind a WS port")
        return 0
    logger.info("PPSSPP started (pid=%d, ws_port=%d)", launcher.pid, ws_port)

    transport: Optional[WsTransport] = None
    try:
        # Step 2: connect a real WsTransport.
        logger.info("connecting WsTransport (port=%d)", ws_port)
        transport = WsTransport(host="127.0.0.1", port=ws_port)
        await transport.connect()
        logger.info("WsTransport connected")

        # Step 3: wrap in RecordingTransport (records to cassette).
        recorder = RecordingTransport(
            real_transport=transport,
            cassette_path=cassette_path,
            capture_state_changes=True,
        )

        # Step 4: drive the 30-tool checklist.
        logger.info("driving %d-tool checklist", len(TOOL_CHECKLIST))
        for desc, event, params in TOOL_CHECKLIST:
            await record_one(recorder, desc, event, params)

        # Step 5: flush to cassette.jsonl.
        logger.info("flushing cassette to %s", cassette_path)
        recorder.flush()
        n_records = len(recorder.records)
        logger.info("cassette written: %d records", n_records)

        # Step 5b: derive per-event fixtures from the cassette.
        # ContractRecorder.from_cassette reads the just-written cassette
        # and groups records by event name, writing one JSON file per
        # event to fixtures/<event>.json. This avoids a second live
        # recording pass — the cassette is the single source of truth.
        # ppsspp_version is left as None so from_cassette auto-extracts
        # it from the cassette's `version` event record.
        logger.info("deriving contract fixtures from cassette")
        contract_recorder = ContractRecorder.from_cassette(cassette_path)
        fixture_counts = contract_recorder.flush(fixtures_dir)
        n_fixtures = sum(fixture_counts.values())
        logger.info(
            "fixtures written: %d events, %d total records to %s "
            "(ppsspp_version=%s)",
            len(fixture_counts), n_fixtures, fixtures_dir,
            contract_recorder._ppsspp_version,
        )

        return n_records

    finally:
        # Step 6: cleanup.
        if transport is not None:
            try:
                await transport.close()
            except Exception as exc:
                logger.warning("WsTransport.close failed: %s", exc)
        logger.info("stopping PPSSPP")
        launcher.stop()
        logger.info("done")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Record real PPSSPP WS communication to cassette + fixtures.",
    )
    parser.add_argument(
        "--iso",
        default=DEFAULT_ISO,
        help=f"ISO file path (default: {DEFAULT_ISO})",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    iso_path = Path(args.iso).resolve()
    if not iso_path.is_file():
        logger.error("ISO not found: %s", iso_path)
        return 2

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Verify PPSSPP exe is configured before starting.
    exe = ppsspp_exe_path()
    if exe is None or not exe.is_file():
        logger.error(
            "PPSSPP exe not found. Configure .ppsspp-dfx/config/project.yaml "
            "or set PPSSPP_DFX_EXE_PATH env var."
        )
        return 3

    n = asyncio.run(run_recording(iso_path, output_dir))
    if n == 0:
        logger.error("recording produced 0 records — something went wrong")
        return 1
    logger.info("recording complete: %d records written", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
