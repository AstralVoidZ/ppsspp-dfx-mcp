"""W3 (review v3): analyze_log must not report truncated=false at the cap.

`_filter_log_lines` breaks at MAX_LOG_MATCHES=500, but the tool computed
`total = len(matches)` / `truncated = total > limit`, so a log with 2000
hits returned count=500, total_matches=500, truncated=false — the caller
believes the list is complete.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ppsspp_dfx_mcp.tools import analyze as analyze_mod
from ppsspp_dfx_mcp.tools._common import MAX_LOG_MATCHES
from ppsspp_dfx_mcp.tools.analyze import analyze_log


def _write_log(tmp_path: Path, n_lines: int) -> Path:
    path = tmp_path / "app.log"
    path.write_text("\n".join(f"ERROR boom {i}" for i in range(n_lines)), encoding="utf-8")
    return path


@pytest.fixture()
def allow_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(analyze_mod, "_LOG_ALLOWED_ROOTS", (tmp_path.resolve(),))
    return tmp_path


async def test_internal_cap_reports_truncated(allow_tmp: Path):
    path = _write_log(allow_tmp, MAX_LOG_MATCHES + 100)
    result = await analyze_log(log_path=str(path))
    assert result["count"] == MAX_LOG_MATCHES
    # Pre-fix: truncated is False (and total_matches claims a complete 500).
    assert result["truncated"] is True
    assert result["total_matches"] == MAX_LOG_MATCHES


async def test_under_cap_is_not_truncated(allow_tmp: Path):
    path = _write_log(allow_tmp, 10)
    result = await analyze_log(log_path=str(path))
    assert result["count"] == 10
    assert result["truncated"] is False
    assert result["total_matches"] == 10


async def test_explicit_limit_still_truncates(allow_tmp: Path):
    path = _write_log(allow_tmp, 20)
    result = await analyze_log(log_path=str(path), limit=5)
    assert result["count"] == 5
    assert result["truncated"] is True
    assert result["total_matches"] == 20
