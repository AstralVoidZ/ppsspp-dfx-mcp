"""Review-v4 W-5: the VRAM fallback's CPU-bound conversion must live in a
pure helper run via asyncio.to_thread — inline, the 557KB blank scan +
130k-iteration pixel loop + PNG encode stalled the whole event loop (every
other session, recv loop and GC) for ~1s while the path already holds the
session lock on a wedged PPSSPP.

PIL is an optional production dependency for this path (capture imports it
inside try/except), so the convert-behavior case is importorskip-gated the
same way the production path degrades; the blank-rejection and structural
offload cases run everywhere.
"""

from __future__ import annotations

import inspect

from ppsspp_dfx_mcp.service.capture import CaptureService, _vram_candidate_to_png


def test_blank_candidate_is_rejected_without_pil() -> None:
    """The cheap blank gate must fire before any optional dependency."""
    assert _vram_candidate_to_png(bytes(512 * 272 * 4), 480, 272, 512, 4) is None


def test_non_blank_candidate_converts_to_png() -> None:
    import io

    pytest = __import__("pytest")
    pytest.importorskip("PIL", reason="PIL is an optional dependency of the VRAM screenshot path")
    from PIL import Image

    raw = bytes([255, 0, 0, 255]) * (512 * 272)
    png = _vram_candidate_to_png(raw, 480, 272, 512, 4)
    assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"
    img = Image.open(io.BytesIO(png))
    assert img.size == (480, 272)


def test_screenshot_offloads_the_conversion_off_the_event_loop() -> None:
    """Structural lock: the pixel loop must never run inline again."""
    source = inspect.getsource(CaptureService._vram_screenshot)
    assert "to_thread(" in source
    assert "for y in range(H)" not in source
