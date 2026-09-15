"""W4 fix tests: PPSSPP broadcast-log mirror + analyze_log default path.

Real-PPSSPP probe evidence: a fresh PpssppLauncher().read_log() returns ""
unconditionally (no construction site ever configures log_path), so
analyze_log(log_path=None) could never produce matches. The fix mirrors
the PPSSPP broadcast log (injected by GameStateObserver into the
"ppsspp_dfx_mcp.ppsspp_log" logger) to .ppsspp-dfx/output/ppsspp.log and
makes the default path read that file.
"""

from __future__ import annotations

import logging

import pytest

from ppsspp_dfx_mcp import config
from ppsspp_dfx_mcp.logging import (
    PPSSPP_LOG_LOGGER_NAME,
    PPSSPPLogMirrorHandler,
    attach_ppsspp_log_mirror,
)
from ppsspp_dfx_mcp.tools.analyze import analyze_log


@pytest.fixture
def mirror_target(tmp_path):
    """Attach a mirror handler writing into tmp_path, with cleanup."""
    target = tmp_path / "ppsspp.log"
    handler = PPSSPPLogMirrorHandler(target)
    handler.setFormatter(logging.Formatter("%(message)s"))
    ppsspp_logger = logging.getLogger(PPSSPP_LOG_LOGGER_NAME)
    ppsspp_logger.addHandler(handler)
    yield target
    ppsspp_logger.removeHandler(handler)


def _emit_ppsspp_log(message: str) -> None:
    logging.getLogger(PPSSPP_LOG_LOGGER_NAME).error(message)


def test_mirror_handler_writes_records(mirror_target):
    _emit_ppsspp_log("[HLE] ERROR: bad syscall 0x1234")
    _emit_ppsspp_log("[GPU] WARNING: framebuffer timeout")
    text = mirror_target.read_text(encoding="utf-8")
    assert "bad syscall 0x1234" in text
    assert "framebuffer timeout" in text
    assert text.count("\n") == 2


def test_mirror_handler_stops_appending_past_cap(tmp_path):
    target = tmp_path / "capped.log"
    handler = PPSSPPLogMirrorHandler(target, max_bytes=64)
    handler.setFormatter(logging.Formatter("%(message)s"))
    try:
        handler.emit(logging.makeLogRecord({"msg": "x" * 100, "levelname": "ERROR"}))
        size_after_first = target.stat().st_size
        handler.emit(logging.makeLogRecord({"msg": "y" * 100, "levelname": "ERROR"}))
        assert target.stat().st_size == size_after_first
    finally:
        pass  # no shared state — handler was never attached to a logger


def test_attach_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "output_dir", lambda: tmp_path)
    from ppsspp_dfx_mcp.logging import PPSSPP_LOG_LOGGER_NAME as _N
    logger = logging.getLogger(_N)
    before = len(logger.handlers)
    path1 = attach_ppsspp_log_mirror()
    try:
        assert path1 == tmp_path / "ppsspp.log"
        path2 = attach_ppsspp_log_mirror()
        assert path2 is None  # skipped — already attached
        assert len(logger.handlers) == before + 1
        assert isinstance(logger.handlers[-1], PPSSPPLogMirrorHandler)
    finally:
        # Remove whatever attach added so other tests are unaffected.
        for h in list(logger.handlers):
            if isinstance(h, PPSSPPLogMirrorHandler):
                logger.removeHandler(h)


@pytest.mark.asyncio
async def test_analyze_log_default_reads_mirror(tmp_path, monkeypatch, mirror_target):
    """Default path (log_path=None) reads the mirrored file when present."""
    monkeypatch.setattr(
        "ppsspp_dfx_mcp.tools.analyze.output_dir", lambda: tmp_path
    )
    _emit_ppsspp_log("ERROR HLE: bad syscall")
    _emit_ppsspp_log("INFO everything fine")
    _emit_ppsspp_log("ERROR gpu: timeout")

    result = await analyze_log(log_path=None, filter=None, session_id=None)
    assert result["count"] == 2
    assert result["log_path"].endswith("ppsspp.log")
    texts = " | ".join(m["text"] for m in result["matches"])
    assert "bad syscall" in texts and "gpu: timeout" in texts


@pytest.mark.asyncio
async def test_analyze_log_default_missing_file(tmp_path, monkeypatch):
    """No mirror file yet → explicit empty result naming the mirror path."""
    monkeypatch.setattr(
        "ppsspp_dfx_mcp.tools.analyze.output_dir", lambda: tmp_path
    )
    result = await analyze_log(log_path=None, filter=None, session_id=None)
    assert result["count"] == 0
    assert result["matches"] == []
    assert "no mirrored ppsspp log" in result["log_path"]
