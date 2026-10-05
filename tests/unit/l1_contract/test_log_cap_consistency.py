"""Cap consistency between the log mirror writer and the log reader.

D1 root cause (measured 2026-09-30):
    The mirror handler's "cap reached" marker was written *past* the cap, so
    the file landed at 10,485,871 bytes while the reader rejects anything
    above 10,485,760. Default `ppsspp_analyze_log` therefore failed
    permanently once the log filled.

    Two independent constants define that number:

        logging.py:19   MIRROR_MAX_BYTES   (writer side)
        tools/_common.py:124  MAX_LOG_BYTES (reader side)

    They are duplicated on purpose: logging.py:16-18 states the duplication
    exists to avoid a core -> tools import. Merging them would invert the
    layering the constitution forbids, so the invariant is enforced by
    this test instead (research.md R-3).

Falsifiable: editing either constant without the other makes this fail.
"""

from __future__ import annotations

from ppsspp_dfx_mcp import logging as dfx_logging
from ppsspp_dfx_mcp.tools._common import MAX_LOG_BYTES, MAX_LOG_MATCHES

CAP = 10 * 1024 * 1024


class TestLogCapConsistency:
    """The writer cap and the reader cap MUST agree."""

    def test_reader_cap_matches_documented_10mib(self) -> None:
        assert MAX_LOG_BYTES == CAP, f"reader cap drifted: {MAX_LOG_BYTES} != {CAP}"

    def test_writer_cap_matches_reader_cap(self) -> None:
        """The invariant that keeps D1 from recurring."""
        assert dfx_logging.MIRROR_MAX_BYTES == MAX_LOG_BYTES, (
            f"log mirror writer cap ({dfx_logging.MIRROR_MAX_BYTES}) != "
            f"log reader cap ({MAX_LOG_BYTES}); the mirror can overshoot and "
            f"lock the reader out"
        )

    def test_marker_budget_fits_inside_cap(self) -> None:
        """The reserved marker space MUST leave room for a line.

        The D1 fix reserves MARKER_MAX_BYTES before writing the cap marker.
        That budget has to be strictly smaller than the cap, otherwise the
        guard would refuse to write the marker at all.
        """
        budget = getattr(dfx_logging, "MARKER_MAX_BYTES", None)
        assert budget is not None, (
            "MARKER_MAX_BYTES missing: the writer does not reserve space for "
            "the cap marker, so the marker can push the file past the cap"
        )
        assert 0 < budget < MAX_LOG_BYTES, (
            f"MARKER_MAX_BYTES={budget} must be in (0, {MAX_LOG_BYTES})"
        )

    def test_match_cap_is_positive(self) -> None:
        """Reader-side match cap: a 0 would silently return nothing."""
        assert MAX_LOG_MATCHES > 0
