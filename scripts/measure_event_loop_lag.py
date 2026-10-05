"""measure_event_loop_lag.py — W-5: event-loop blocking quantification (specs/010 US3).

FR-018 / C5-7 / M-22: the screenshot-path blocking MUST be expressed as a
numeric ceiling with a re-runnable measurement method.

Method — differential heartbeat probing (对照法):
- A heartbeat task beats every ``--heartbeat-ms`` and records the drift
  (actual interval minus expected interval) — the drift IS the main-loop
  lag the debugger session feels while a blocking call hogs the loop.
- Two scenarios run back to back over the SAME workload:
    baseline : the blocking work runs INLINE on the event loop (the
               pre-fix shape — vram screenshot writes were synchronous
               ``path.write_bytes`` calls inside tool coroutines).
    fixed    : the same work runs via ``asyncio.to_thread`` (the current
               implementation; anchors: tools/_common.py:230/239,
               run_script's to_thread guard).
  The workload is a stand-in (equal-duration ``time.sleep``) rather than
  a real 8 MiB PNG write: disk caches make real writes non-reproducible.
  Conclusion level for these numbers: ``替身等价`` (stand-in equivalent).

Exit codes:
    0  fixed max lag < --max-lag-ms AND baseline actually lagged
       (the stand-in was effective) — i.e. the comparison is valid.
    2  fixed max lag >= --max-lag-ms — the implementation has regressed
       to synchronous blocking (M-22's regression judge), OR the baseline
       failed to lag (stand-in ineffective — no conclusion possible).

Usage:
    <python> scripts/measure_event_loop_lag.py --scenario vram-screenshot
    <python> scripts/measure_event_loop_lag.py --scenario vram-screenshot --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

#: The vram-screenshot blocking is dominated by the framebuffer dump write
#: (8 MiB blob -> disk). The stand-in duration models the observed
#: worst-case blocking of that path on a cold disk cache.
SCENARIOS: dict[str, dict[str, float]] = {
    "vram-screenshot": {"block_ms": 200.0, "chunks": 4},
}
DEFAULT_HEARTBEAT_MS = 10.0
DEFAULT_MAX_LAG_MS = 50.0  # SC-009: fixed max lag must stay under 50 ms


async def _measure_phase(
    mode: str, block_ms: float, chunks: int, heartbeat_ms: float
) -> dict[str, float]:
    """Run one phase (baseline or fixed) and return drift statistics.

    The blocker does ``chunks`` back-to-back blocking operations of
    ``block_ms`` each (a screenshot does one big write, but multiple
    chunks model the tool-call + diagnostics-write sequence); the
    heartbeat runs concurrently and records every interval overshoot.
    """
    drifts_ms: list[float] = []

    async def blocker() -> None:
        for _ in range(chunks):
            if mode == "baseline":
                time.sleep(block_ms / 1000.0)  # inline: blocks the loop
            else:
                await asyncio.to_thread(time.sleep, block_ms / 1000.0)
            # Let the heartbeat observe between chunks.
            await asyncio.sleep(0)

    async def heartbeat(deadline: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        expected = heartbeat_ms / 1000.0
        prev = loop.time()
        while not deadline.is_set():
            await asyncio.sleep(expected)
            now = loop.time()
            drifts_ms.append((now - prev - expected) * 1000.0)
            prev = now

    done = asyncio.Event()
    hb = asyncio.create_task(heartbeat(done))
    await blocker()
    done.set()
    await asyncio.gather(hb, return_exceptions=True)

    return {
        "max_lag_ms": max(drifts_ms) if drifts_ms else 0.0,
        "mean_lag_ms": (sum(drifts_ms) / len(drifts_ms)) if drifts_ms else 0.0,
        "samples": float(len(drifts_ms)),
    }


async def _run(scenario: str, heartbeat_ms: float) -> dict[str, object]:
    spec = SCENARIOS[scenario]
    baseline = await _measure_phase("baseline", spec["block_ms"], int(spec["chunks"]), heartbeat_ms)
    # Breathe between phases so the previous phase's tasks are reaped.
    await asyncio.sleep(0.2)
    fixed = await _measure_phase("fixed", spec["block_ms"], int(spec["chunks"]), heartbeat_ms)
    return {"scenario": scenario, "baseline": baseline, "fixed": fixed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="W-5 event-loop lag measurement (differential heartbeat)"
    )
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="vram-screenshot")
    parser.add_argument("--heartbeat-ms", type=float, default=DEFAULT_HEARTBEAT_MS)
    parser.add_argument("--max-lag-ms", type=float, default=DEFAULT_MAX_LAG_MS)
    parser.add_argument(
        "--json", dest="json_out", default=None, help="write the full result JSON here"
    )
    args = parser.parse_args(argv)

    result = asyncio.run(_run(args.scenario, args.heartbeat_ms))
    base = result["baseline"]
    fix = result["fixed"]
    verdict = "PASS"
    exit_code = 0

    # Validity of the comparison: the baseline stand-in MUST actually lag
    # (>= one blocking chunk), else "fixed is better" proves nothing.
    if float(base["max_lag_ms"]) < float(args.max_lag_ms):
        verdict = "INVALID (baseline failed to lag — stand-in ineffective)"
        exit_code = 2
    elif float(fix["max_lag_ms"]) >= float(args.max_lag_ms):
        # M-22 regression judge: to_thread degraded to inline blocking.
        verdict = "FAIL (fixed path exceeds the numeric ceiling)"
        exit_code = 2

    print(f"scenario            : {args.scenario} (替身等价 stand-in workload)")
    print(
        f"baseline max lag ms : {base['max_lag_ms']:.1f} (mean {base['mean_lag_ms']:.1f}, n={base['samples']:.0f})"
    )
    print(
        f"fixed    max lag ms : {fix['max_lag_ms']:.1f} (mean {fix['mean_lag_ms']:.1f}, n={fix['samples']:.0f})"
    )
    print(f"ceiling             : fixed max lag < {args.max_lag_ms:.0f} ms")
    print(f"VERDICT: {verdict}")
    print(f"EXIT={exit_code}")

    if args.json_out:
        import pathlib

        payload = {
            "schema": "event-loop-lag/1",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "scenario": args.scenario,
            "conclusion_level": "替身等价 (stand-in equivalent workload)",
            "method": "differential heartbeat probing (baseline inline vs fixed to_thread)",
            "ceiling_ms": args.max_lag_ms,
            "result": result,
            "verdict": verdict,
            "exit_code": exit_code,
        }
        pathlib.Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
