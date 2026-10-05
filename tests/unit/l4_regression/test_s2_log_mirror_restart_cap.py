"""S2 regression lock: the log mirror cap must survive a server restart.

D1 (2026-09-30) reserved ``MARKER_MAX_BYTES`` up front so the mirror can
never cross ``MIRROR_MAX_BYTES``. That reservation only holds if the cap
marker is written at most once per FILE — but "already marked" was tracked by
an attribute on the handler, i.e. per process. The normal lifecycle of this
server is start → stop → start, so the second run appended one more marker to
a mirror that was already inside the reserved band:

    10,485,736 (marker written by run 1)
  +          104 (marker written by run 2)
  =    10,485,840 > 10,485,760 (the reader's cap)

``ppsspp_analyze_log`` rejects anything above the cap and no argument
recovers, so its default path was permanently broken again — the exact
failure D1 claimed to have fixed. The writer now decides from the file's own
size, and ``attach_ppsspp_log_mirror`` rotates a file that arrived oversized.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ppsspp_dfx_mcp import logging as dfx_logging
from ppsspp_dfx_mcp.logging import (
    COMPACT_CAP_MARKER,
    MARKER_MAX_BYTES,
    MIRROR_MAX_BYTES,
    PPSSPPLogMirrorHandler,
    _rotate_oversized_mirror,
)

CAP = 400


def _handler(path: Path, cap: int = CAP) -> PPSSPPLogMirrorHandler:
    h = PPSSPPLogMirrorHandler(path, max_bytes=cap)
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


def _emit(h: PPSSPPLogMirrorHandler, msg: str) -> None:
    h.emit(logging.LogRecord("t", logging.INFO, __file__, 1, msg, None, None))


def _fill_to_cap(path: Path, cap: int = CAP) -> PPSSPPLogMirrorHandler:
    """Run one 'process' worth of records until the mirror is capped."""
    h = _handler(path, cap)
    for i in range(50):
        _emit(h, f"line-{i:03d} " + "x" * 180)
    return h


class TestRestartDoesNotOvershoot:
    def test_second_process_stays_under_the_cap(self, tmp_path) -> None:
        """The measured S2 defect: run 2 pushed the mirror past the cap."""
        log = tmp_path / "m.log"
        _fill_to_cap(log)
        size_after_run1 = log.stat().st_size
        assert size_after_run1 <= CAP

        # A fresh handler == a fresh process (the flag does not survive).
        restarted = _handler(log)
        for i in range(20):
            _emit(restarted, f"restart-{i:03d} " + "y" * 180)

        size_after_run2 = log.stat().st_size
        assert size_after_run2 <= CAP, (
            f"restart pushed the mirror to {size_after_run2} bytes (cap {CAP}) — "
            "the reader would reject the default log path forever"
        )

    def test_file_arriving_inside_the_reserved_band_is_left_alone(self, tmp_path) -> None:
        """The exact measured state: cap − 24 bytes, marker already present."""
        log = tmp_path / "m.log"
        marker = (
            f"\n{dfx_logging.CAP_MARKER_PREFIX} at {CAP} bytes on "
            "2026-09-30 12:00:00 — further records suppressed.\n"
        )
        # Simulate run 1: the file sits just under the cap with its marker at
        # the end — no room left for another full marker.
        filler = "x" * (CAP - len(marker.encode("utf-8")) - 24)
        log.write_text(filler + marker, encoding="utf-8")
        before = log.stat().st_size
        assert before + len(marker.encode("utf-8")) > CAP  # the pre-fix overshoot

        handler = _handler(log)
        for i in range(20):
            _emit(handler, f"after-restart-{i} " + "z" * 180)

        assert log.stat().st_size <= CAP, (
            "a mirror that arrived inside the reserved band was grown past the cap"
        )
        text = log.read_text(encoding="utf-8", errors="replace")
        assert text.count(dfx_logging.CAP_MARKER_PREFIX) == 1, (
            "the restart appended a second cap marker"
        )

    def test_compact_marker_is_used_when_the_full_one_does_not_fit(self, tmp_path) -> None:
        """Capping must stay visible even when only a short notice fits."""
        log = tmp_path / "m.log"
        h = _handler(log, cap=MARKER_MAX_BYTES + 10)
        _emit(h, "x" * 200)
        text = log.read_text(encoding="utf-8", errors="replace")
        assert dfx_logging.CAP_MARKER_PREFIX in text
        assert log.stat().st_size <= MARKER_MAX_BYTES + 10
        assert len(COMPACT_CAP_MARKER.encode("utf-8")) < MARKER_MAX_BYTES

    def test_real_cap_never_has_room_for_a_second_marker(self) -> None:
        """Arithmetic lock at the shipped constants.

        The measured D1/S2 state is a mirror 24 bytes under the cap with its
        marker already written. Neither the full marker nor the compact one
        fits in 24 bytes, so a restart cannot grow that file at all.
        """
        full = _handler(Path("unused.log"))._full_cap_marker()
        assert len(full.encode("utf-8")) <= MARKER_MAX_BYTES, (
            "the reserved budget no longer covers the real marker text"
        )

        measured_state = MIRROR_MAX_BYTES - 24
        assert measured_state + len(full.encode("utf-8")) > MIRROR_MAX_BYTES
        assert measured_state + len(COMPACT_CAP_MARKER.encode("utf-8")) > MIRROR_MAX_BYTES


class TestRotateOversizedMirror:
    def test_oversized_mirror_is_rotated_aside(self, tmp_path) -> None:
        log = tmp_path / "ppsspp.log"
        log.write_bytes(b"x" * (CAP + 1))

        rotated = _rotate_oversized_mirror(log, CAP)

        assert rotated == tmp_path / "ppsspp.log.1"
        assert rotated is not None and rotated.stat().st_size == CAP + 1
        assert not log.exists(), "the broken mirror must not stay at the default path"

    def test_rotation_replaces_an_older_rotation(self, tmp_path) -> None:
        log = tmp_path / "ppsspp.log"
        log.write_bytes(b"y" * (CAP + 1))
        (tmp_path / "ppsspp.log.1").write_bytes(b"stale")

        rotated = _rotate_oversized_mirror(log, CAP)

        assert rotated is not None
        assert rotated.read_bytes() == b"y" * (CAP + 1)

    def test_file_at_or_under_the_cap_is_not_rotated(self, tmp_path) -> None:
        log = tmp_path / "ppsspp.log"
        log.write_bytes(b"x" * CAP)

        assert _rotate_oversized_mirror(log, CAP) is None
        assert log.stat().st_size == CAP
