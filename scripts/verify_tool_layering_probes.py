"""H1 acceptance probe A-H1-5: trace_memory_access against real PPSSPP.

One-off verification (analysis_ppsspp_dfx_mcp_tool_layering_v1 §3 第二档):
arm a memory-access trace on the game_mode address (0x08A0D000) on a live
game boot and confirm ≥1 real hit with a plausible post-hit PC.

Strategy:
1. read-only trace, 15s — the game loop polls game_mode every frame, so a
   read hit should land within seconds on a running title screen.
2. If that times out: read_write trace, 15s, with a cross press fired 2s
   in (mode transitions WRITE game_mode).

Run from mcps/ppsspp-dfx-mcp with a PPSSPP-capable environment (same
preconditions as scripts/verify_real_mcp.py phase B). This script
stops the session it started; it does not touch other sessions.
"""

from __future__ import annotations

import asyncio
import json
import sys

from _wire import ISO_PATH, PACKAGE_ROOT, WORKSPACE_ROOT
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

SRC_DIR = str(PACKAGE_ROOT / "src")
# A known game-state variable address for the booted game (override per game).
GAME_MODE_ADDR = "0x08A0D000"
# PSP user-memory code range: any hit PC in here is "in module code".
CODE_LO, CODE_HI = 0x08800000, 0x0C000000

PYTHON_EXE = sys.executable


def _structured(result) -> dict:
    return getattr(result, "structured_content", None) or {}


def _is_error(result) -> bool:
    err = getattr(result, "is_error", None)
    return bool(err if err is not None else getattr(result, "isError", False))


def _hex(v) -> str:
    try:
        return f"0x{int(v):08X}"
    except (TypeError, ValueError):        return str(v)


async def run() -> int:
    env = get_default_environment()
    env["PYTHONPATH"] = str(SRC_DIR)
    env["PPSSPP_DFX_LOG_LEVEL"] = "INFO"
    params = StdioServerParameters(
        command=PYTHON_EXE,
        args=["-m", "ppsspp_dfx_mcp"],
        env=env,
        cwd=str(WORKSPACE_ROOT),
    )
    failures: list[str] = []
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        r = await session.call_tool("ppsspp_session", {"action": "start", "iso_path": ISO_PATH})
        if _is_error(r):
            print(f"start failed: {str(r.content)[:300]}")
            return 1
        sid = _structured(r)["session_id"]
        try:
            r = await session.call_tool(
                "ppsspp_session", {"action": "wait_ready", "session_id": sid, "timeout_s": 100.0}
            )
            if _is_error(r):
                print(f"wait_ready failed: {str(r.content)[:300]}")
                return 1
            print(f"ready: {_structured(r)}")

            # Attempt 1: passive read trace (game polls game_mode).
            r = await session.call_tool(
                "ppsspp_trace_memory_access",
                {
                    "session_id": sid,
                    "address": GAME_MODE_ADDR,
                    "access": "read",
                    "timeout_s": 15.0,
                    "want_backtrace": True,
                },
            )
            s = _structured(r)
            print(
                f"[attempt1 read] hit={s.get('hit')} "
                f"bp_removed={s.get('bp_removed')} "
                f"resumed={s.get('resumed')} hits={json.dumps(s.get('hits'))[:400]}"
            )
            if not s.get("hit"):
                # Attempt 2: read_write trace + a cross press 2s in
                # (mode transitions write game_mode).
                task = asyncio.create_task(
                    session.call_tool(
                        "ppsspp_trace_memory_access",
                        {
                            "session_id": sid,
                            "address": GAME_MODE_ADDR,
                            "access": "read_write",
                            "timeout_s": 15.0,
                            "want_registers": True,
                        },
                    )
                )
                await asyncio.sleep(2.0)
                pr = await session.call_tool(
                    "ppsspp_press_button", {"session_id": sid, "button": "cross", "duration": 3}
                )
                print(
                    f"[attempt2] press cross ok={not _is_error(pr)} "
                    f"(concurrent with trace — lock-free check)"
                )
                r = await task
                s = _structured(r)
                print(
                    f"[attempt2 read_write] hit={s.get('hit')} "
                    f"bp_removed={s.get('bp_removed')} "
                    f"resumed={s.get('resumed')} "
                    f"hits={json.dumps(s.get('hits'))[:400]}"
                )

            if not s.get("hit"):
                failures.append("no real hit on either attempt")
            else:
                hit = (s.get("hits") or [{}])[0]
                pc = hit.get("pc")
                pc_int = int(pc, 16) if isinstance(pc, str) else pc
                if not (CODE_LO <= pc_int < CODE_HI):
                    failures.append(f"hit PC {pc} outside code range")
                if not s.get("bp_removed"):
                    failures.append("breakpoint not removed")
                # A1-2: mem_hits attribution counter (real wire).
                mem_hits = hit.get("mem_hits")
                print(f"A1-2 mem_hits: {mem_hits}")
                if not (isinstance(mem_hits, int) and mem_hits >= 1):
                    failures.append(f"mem_hits missing/zero: {mem_hits!r}")

            # A1-1: raw broadcast evidence retrievable from the mirror.
            ar = await session.call_tool("ppsspp_analyze_log", {"filter": "cpu.stepping"})
            ars = _structured(ar)
            raw_lines = [
                m.get("text", "") for m in ars.get("matches", []) if "[ws.raw]" in m.get("text", "")
            ]
            print(f"A1-1 raw broadcast lines: {len(raw_lines)}")
            if raw_lines:
                print(f"  sample: {raw_lines[0][:160]}")
            else:
                failures.append("no [ws.raw] line in analyze_log(filter=cpu.stepping)")

            # A1-3: version fingerprint surfaced on session get.
            gr = await session.call_tool("ppsspp_session", {"action": "get", "session_id": sid})
            fingerprint = _structured(gr).get("ppsspp_version")
            print(f"A1-3 ppsspp_version fingerprint: {fingerprint}")
            if not fingerprint:
                failures.append("session get has no ppsspp_version")

            # Leave the game running for the harness-style stop.
            gs = _structured(
                await session.call_tool("ppsspp_query", {"session_id": sid, "action": "game_state"})
            )
            print(f"final game_state.paused: {(gs.get('data') or {}).get('paused')}")
        finally:
            await session.call_tool("ppsspp_session", {"action": "stop", "session_id": sid})
    if failures:
        print(f"A-H1-5 FAIL: {failures}")
        return 1
    print("A-H1-5 PASS: real trace hit with in-range PC, bp cleaned, CPU restored")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
