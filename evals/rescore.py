"""Offline re-score of stored B1 trajectories under v1.2 scenario cards.

The 95-grid B1 archive was scored under v1.1 gates; v1.2 whitelisted
ppsspp_session (then ppsspp_session_list; merged into session in v0.1.6)
in L2-03/L2-04 first_tool. gates.evaluate is pure, so
stored tool_calls + final_answer can be re-scored against the CURRENT
scenarios.yaml without re-running.

Usage (from mcps/ppsspp-dfx-mcp/):
  <venv python> -m evals.rescore --archive evals/runs/archive/runs-20260913-ark-baseline.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from evals.gates import evaluate

_EVALS_DIR = Path(__file__).resolve().parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", required=True)
    ap.add_argument("--scenarios", default="L1-01,L1-07,L1-08,L2-02,L2-03,L2-04,L3-01,L3-03")
    args = ap.parse_args()

    scen_cfg = yaml.safe_load((_EVALS_DIR / "scenarios.yaml").read_text(encoding="utf-8"))
    meta = {s["id"]: s for s in scen_cfg["scenarios"]}
    fixtures_dir = (_EVALS_DIR / scen_cfg["fixtures_dir"]).resolve()
    wanted = set(args.scenarios.split(","))

    out_path = _EVALS_DIR / "runs" / "rescored-b1-v12.jsonl"
    n_ok = n_total = 0
    with (
        Path(args.archive).open(encoding="utf-8") as f,
        out_path.open("w", encoding="utf-8") as out,
    ):
        for line in f:
            if not line.strip():
                continue
            r: dict[str, Any] = json.loads(line)
            sid = r.get("scenario_id", "")
            if sid not in wanted or r.get("variant") != "B1":
                continue
            rescored = evaluate(meta[sid], r, fixtures_dir)
            r["gates"] = {g["type"]: g["pass"] for g in rescored["gates"]}
            r["gate_details"] = rescored["gates"]
            r["success"] = rescored["success"]
            out.write(json.dumps(r, ensure_ascii=False) + "\n")
            n_total += 1
            n_ok += int(r["success"])
    print(f"rescored {n_total} B1 runs under v1.2 gates → {out_path} ({n_ok} pass)")


if __name__ == "__main__":
    main()
