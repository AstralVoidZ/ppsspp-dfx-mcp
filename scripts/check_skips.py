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
from pathlib import Path

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
    # capture's VRAM fallback treats Pillow as an optional dependency
    # (imports it inside try/except and degrades to b""); the convert
    # behavior is only assertable where the optional import resolves.
    r"PIL is an optional dependency",
    # The enum/numeric contract cases source their expectations from the
    # project-side recipe `tools/topx_diagnostics/recipe/device_walkthrough.py`,
    # which is deliberately NOT part of the published package. In a
    # package-only checkout those six cases cannot resolve it and skip
    # explicitly (the test names the case in its message), so this is a
    # deliberate exemption — not a silent path-resolution artifact.
    r"device_walkthrough\.py not found \(package-only checkout\)",
]


def main() -> int:
    # 审计 MUST 锚定包根：`pytest tests` 的 pathspec 相对 cwd，而本脚本允许从
    # 仓库任何位置调用。曾从仓库根调用时 `tests` 解析到不存在的目录，pytest
    # rc=4、零输出 —— 旧实现把「零输出」当「零跳过」，审计假绿（N-3 同类）。
    pkg_root = Path(__file__).resolve().parents[1]

    # 优先用包内 venv 的解释器：测试依赖装在那里；系统解释器大概率没有 pytest，
    # 那同样是「没跑成」而非「零跳过」。
    python = sys.executable
    for cand in (
        pkg_root / ".venv-test" / "Scripts" / "python.exe",
        pkg_root / ".venv-test" / "bin" / "python",
        pkg_root / ".venv" / "Scripts" / "python.exe",
        pkg_root / ".venv" / "bin" / "python",
    ):
        if cand.is_file():
            python = str(cand)
            break

    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [python, "-m", "pytest", "tests", "-q", "-rs", "--no-header"],
        cwd=str(pkg_root),
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr

    # 先验证「运行确实发生了」，再对结果下结论：rc 必须是 0/1，且输出里必须
    # 出现 pytest 的总结行。零输出/收集失败不等于零跳过。
    collected = re.search(r"(\d+) tests collected", output)
    ran = proc.returncode in (0, 1) and (
        " passed" in output or "no tests ran" in output or collected is not None
    )
    if not ran:
        print(
            f"skip audit FAILED: pytest 未正常完成（rc={proc.returncode}）。\n"
            "  零输出/收集失败不等于零跳过 —— 审计结论必须建立在真实运行之上。\n"
            "  --- pytest 输出末尾 ---\n" + "\n".join(output.splitlines()[-15:])
        )
        return 1

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
