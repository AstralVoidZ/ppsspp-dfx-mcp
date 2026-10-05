"""verify_cancel_storm_real.py — W-7 real-device half (specs/010 T042, FR-020).

Drives the cancellation storm against a LIVE PPSSPP debugger connection and
records the conclusion level. Complements tests/integration/test_cancel_storm.py
(the permanent stand-in judges); this script is the real-device half because
the storm's outcome on real hardware depends on uncontrolled timing (loopback
round-trips can beat a 1 ms timeout, PPSSPP GC pauses can exceed it), so a
hard "N timeouts" assertion would be flaky by construction. What IS stable —
and what this script asserts — is the cancellation CONTRACT:

  - every call ENDS (no hang) regardless of timeout/cancel timing,
  - no pending ticket survives the storm,
  - the recv loop survives, and
  - a real call still succeeds end-to-end afterwards.

Environment (real-device gate):
    PPSSPP_DFX_TEST_EXE_PATH / PPSSPP_DFX_TEST_ISO_PATH  - resources
    PPSSPP_DFX_ALLOW_REMOTE_DEBUGGER=1                    - if the debugger
        binds non-loopback (measured 2026-10-04: PPSSPPWindows64 v1.20.4
        binds 0.0.0.0 even with RemoteDebuggerLocal=True in all three
        standard ini locations -- the fail-closed guard then requires this
        explicit acceptance).

Exit codes: 0 = contract holds; 1 = contract violation; 2 = gate not
configured (no env) -- skip, not evidence.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ppsspp_dfx_mcp.core.launcher import PpssppLauncher  # noqa: E402
from ppsspp_dfx_mcp.core.transport import WsTransport  # noqa: E402

STORM, CANCEL, DELAY = 32, 8, 0.05


async def _storm_and_recover(port: int) -> dict[str, object]:
    t1 = WsTransport("127.0.0.1", port)
    await t1.connect()
    r1 = await asyncio.gather(
        *(t1.call("cpu.status", timeout=0.001) for _ in range(STORM)),
        return_exceptions=True,
    )
    storm1 = dict(Counter(type(r).__name__ for r in r1))
    storm1_pending = len(t1._pending)
    storm1_recv_alive = not t1._recv_task.done()
    await t1.close()
    await asyncio.sleep(1.0)

    t2 = WsTransport("127.0.0.1", port)
    await t2.connect()
    try:
        r2 = await asyncio.gather(
            *(t2.call("cpu.status", timeout=0.001) for _ in range(STORM)),
            return_exceptions=True,
        )
        storm2 = dict(Counter(type(r).__name__ for r in r2))
        tasks = [asyncio.create_task(t2.call("cpu.status", timeout=10)) for _ in range(CANCEL)]
        await asyncio.sleep(DELAY)
        for t in tasks:
            t.cancel()
        rr = await asyncio.gather(*tasks, return_exceptions=True)
        cancelled = dict(Counter(type(r).__name__ for r in rr))
        pending = len(t2._pending)
        recv_alive = not t2._recv_task.done()
        recovery: dict[str, object] | None = None
        recovery_error: str | None = None
        try:
            resp = await asyncio.wait_for(t2.call("cpu.status", timeout=10), timeout=15)
            recovery = {"keys": sorted(resp)[:6]}
        except Exception as exc:  # noqa: BLE001
            recovery_error = f"{type(exc).__name__}: {exc}"
        return {
            "storm1": storm1,
            "storm1_pending": storm1_pending,
            "storm1_recv_alive": storm1_recv_alive,
            "storm2": storm2,
            "cancelled": cancelled,
            "pending_after": pending,
            "recv_alive_after": recv_alive,
            "recovery": recovery,
            "recovery_error": recovery_error,
        }
    finally:
        with contextlib.suppress(Exception):
            await t2.close()


def main() -> int:
    exe = os.environ.get("PPSSPP_DFX_TEST_EXE_PATH", "")
    iso = os.environ.get("PPSSPP_DFX_TEST_ISO_PATH", "")
    if not exe or not iso or not Path(exe).is_file() or not Path(iso).is_file():
        print("SKIP: real-device resources not configured (exit 2 = not evidence)")
        return 2

    async def run() -> tuple[int, dict[str, object]]:
        launcher = PpssppLauncher(exe_path=Path(exe))
        await launcher.start(Path(iso), wait_seconds=8.0)
        port = launcher.ws_port
        print(f"[setup] PPSSPP up, ws_port={port}", flush=True)
        try:
            result = await _storm_and_recover(port)
            return 0, result
        finally:
            launcher.stop()

    rc, result = asyncio.run(run())

    # Contract judgement (timing-independent): every call ended, nothing
    # stranded, recv loop alive, real call succeeds afterwards.
    ok = (
        result["storm1_pending"] == 0
        and result["pending_after"] == 0
        and result["storm1_recv_alive"] is True
        and result["recv_alive_after"] is True
        and result["recovery_error"] is None
        and result["recovery"] is not None
    )
    verdict = "PASS" if ok else "FAIL"
    exit_code = 0 if ok else 1

    payload = {
        "schema": "cancel-storm-real/1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ppsspp_build": "v1.20.4 (tools/ppsspp_dev, mtime 2026-05-16)",
        "iso": Path(os.environ.get("PPSSPP_DFX_TEST_ISO_PATH", "game.iso")).name,
        "storm_size": STORM,
        "cancel_size": CANCEL,
        "result": result,
        "conclusion_level": "实测（真机，含构建号 v1.20.4）",
        "verdict": verdict,
        "exit_code": exit_code,
    }
    out = (
        Path(__file__).resolve().parents[3]
        / "mcp_test_report"
        / "derived"
        / "cancel-storm-real-010.json"
    )
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"VERDICT: {verdict}")
    print(f"EXIT={exit_code}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
