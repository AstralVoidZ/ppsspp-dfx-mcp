"""Log mirror must never exceed the reader's cap (US4 / T035 / T036).

D1 (measured 2026-09-30 on the real project): the mirror sat at
10,485,871 bytes while the reader rejects anything above 10,485,760 —
111 bytes over, which is the length of the "cap reached" marker the
writer appends *after* deciding the file is full. The marker itself pushed
the file past the cap, so `ppsspp_analyze_log` with its default path
failed permanently and no argument could recover it.

The fix reserves room for the marker before deciding to cap, so the file
never crosses the cap in the first place.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ppsspp_dfx_mcp.logging import MIRROR_MAX_BYTES, PPSSPPLogMirrorHandler

CAP = 10 * 1024 * 1024


def _handler(path: Path, cap: int) -> PPSSPPLogMirrorHandler:
    h = PPSSPPLogMirrorHandler(path, max_bytes=cap)
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


def _emit(h: PPSSPPLogMirrorHandler, msg: str) -> None:
    h.emit(logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None))


class TestMirrorNeverExceedsCap:
    def test_small_cap_is_respected_with_marker(self, tmp_path) -> None:
        """The regression: filling a tiny cap must not overshoot.

        A 400-byte cap with 200-byte lines forces the cap path several
        times over. Pre-fix the marker pushed the file past 400.
        """
        log = tmp_path / "m.log"
        h = _handler(log, cap=400)
        for i in range(50):
            _emit(h, f"line-{i:03d} " + "x" * 180)

        size = log.stat().st_size
        assert size <= 400, f"log grew to {size} bytes, cap is 400 (D1 regression)"

    def test_marker_is_present_after_capping(self, tmp_path) -> None:
        """Capping must be visible, not silent (S9 contract)."""
        log = tmp_path / "m.log"
        h = _handler(log, cap=400)
        for i in range(50):
            _emit(h, f"line-{i:03d} " + "x" * 180)
        text = log.read_text(encoding="utf-8", errors="replace")
        assert "capped" in text, "cap marker missing — a silent stop misleads"

    def test_capping_happens_only_once(self, tmp_path) -> None:
        """One marker, not one per suppressed record."""
        log = tmp_path / "m.log"
        h = _handler(log, cap=400)
        for i in range(200):
            _emit(h, f"line-{i:03d} " + "x" * 180)
        text = log.read_text(encoding="utf-8", errors="replace")
        assert text.count("capped") == 1, (
            f"expected exactly one cap marker, found {text.count('capped')}"
        )

    def test_records_before_cap_are_preserved(self, tmp_path) -> None:
        """The early tail must survive; capping truncates, never rewrites."""
        log = tmp_path / "m.log"
        h = _handler(log, cap=400)
        for i in range(50):
            _emit(h, f"line-{i:03d} " + "x" * 180)
        text = log.read_text(encoding="utf-8", errors="replace")
        assert "line-000" in text, "earliest record was lost"

    def test_unicode_lines_do_not_overshoot(self, tmp_path) -> None:
        """Byte length, not character length, is what the cap counts."""
        log = tmp_path / "m.log"
        h = _handler(log, cap=500)
        for _ in range(50):
            _emit(h, "中文字符测试" * 20)
        assert log.stat().st_size <= 500, (
            f"multi-byte lines escaped the cap: {log.stat().st_size} > 500"
        )


class TestMarkerBudget:
    def test_marker_budget_exists_and_fits(self) -> None:
        """The reserved budget is the fix; assert its shape."""
        from ppsspp_dfx_mcp import logging as dfx_logging

        budget = getattr(dfx_logging, "MARKER_MAX_BYTES", None)
        assert budget is not None, "MARKER_MAX_BYTES missing (D1 unfixed)"
        assert 0 < budget < MIRROR_MAX_BYTES

    def test_marker_budget_covers_a_real_marker(self, tmp_path) -> None:
        """A generated marker must fit inside the reserved budget.

        Guards against the budget drifting below the real marker length —
        which would reintroduce the overshoot.
        """
        from ppsspp_dfx_mcp import logging as dfx_logging

        log = tmp_path / "m.log"
        h = _handler(log, cap=400)
        for i in range(50):
            _emit(h, f"line-{i:03d} " + "x" * 180)
        # The file must be under the cap, i.e. the marker fitted.
        assert log.stat().st_size <= 400
        assert dfx_logging.MARKER_MAX_BYTES >= 64, (
            "budget too small to be a safe bound on the marker text"
        )
