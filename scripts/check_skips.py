"""CI skip audit (C4, review v2): every SKIPPED test must match a documented reason.

A skip whose reason is an environment-path artifact (e.g. a config dir
resolved outside the repo, a workspace asset only this machine has) zeroes
a whole contract module while CI stays green — that is worse than no gate.
The old `parents[5]` completion-contract path did exactly that on every
fresh clone. This audit fails the job unless every skip reason matches the
allowlist below, so a new undocumented skip can only land deliberately.

Usage (from repo root): python scripts/check_skips.py
Exit 0 = all skips documented; exit 1 = at least one undocumented skip.
"""

from __future__ import annotations

import re
import subprocess
import sys

# Documented, deliberate skip reasons (substring regexes):
#  * real-PPSSPP integration resources are user-local by design;
#  * platform-gated unit tests;
#  * recorded cassette assets are not committed;
#  * the rewired-script integration test needs a workspace-owned script.
ALLOWLIST = [
    r"PPSSPP executable not configured",
    r"Game ISO not configured",
    r"POSIX-only test",
    r"Windows-only test",
    r"real fixtures not recorded yet",
    r"workspace rewired script not present",
    r"gpu\.getStats not supported",  # upstream PPSSPP build limitation
]


def main() -> int:
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-m", "pytest", "tests", "-q", "-rs", "--no-header"],
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    lines = [ln for ln in output.splitlines() if ln.startswith("SKIPPED")]
    bad = [ln for ln in lines if not any(re.search(pat, ln) for pat in ALLOWLIST)]
    print(
        f"skip audit: {len(lines)} skipped "
        f"({len(lines) - len(bad)} documented, {len(bad)} UNDOCUMENTED)"
    )
    for ln in bad:
        print("  UNDOCUMENTED SKIP:", ln)
    if bad:
        print(
            "\nA skip must be a deliberate, documented exemption — not a "
            "silent path-resolution artifact.\nEither fix the test to "
            "resolve its assets repo-internally, or add its reason to the "
            "ALLOWLIST in scripts/check_skips.py with a justification."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
