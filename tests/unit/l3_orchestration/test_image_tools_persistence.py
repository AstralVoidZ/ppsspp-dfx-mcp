"""L3 orchestration: image tools' auto-save / output-dir persistence contract.

Anchor: openspec capability `imagecontent-output`, task 4.6 of change
`ppsspp-dfx-mcp-protocol-and-schema`.

Tasks 4.1-4.4 rewrote all three image tools from a
``[json.dumps(meta), Image(...)]`` list to a hand-built ``CallToolResult``
(ImageContent in ``content``, metadata in ``structuredContent``). That is a
BREAKING wire-shape change, so the persistence side channel needs its own
lock: ``file_path`` must point at a file that **actually exists** under
``output/<subdir>/`` (it is the only way a caller re-reads a capture without
re-capturing), and the subdir must be created on demand.

Why a new file rather than extending an existing one: before this, nothing
in ``tests/`` asserted anything about ``output/screenshots/``, ``textures/``
or ``cluts/`` (the spec requirements had zero coverage), so a regression in
the rewrite would have been silent.

Covered requirements (all from `imagecontent-output`):
- screenshot auto-saves to output directory
- output directory auto-created
- dump_texture auto-saves to output directory
- dump_clut auto-saves (ADDED by this change)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from mcp.types import CallToolResult, ImageContent

from ppsspp_dfx_mcp.tools import screenshot as sc

pytestmark = pytest.mark.asyncio

# A 24-byte PNG header: 8-byte signature, 8 filler bytes, then the IHDR
# width/height at the offsets `_image_dims` reads (16:20 / 20:24).
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (64).to_bytes(4, "big") + (32).to_bytes(4, "big")
assert len(_PNG) == 24  # _image_dims bails below 24 bytes.


def _patch_output_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect .ppsspp-dfx/output to a pytest tmp dir (returns the root).

    Targets `tools._common.output_dir` — `save_output_bytes` resolves the
    name from its own module globals, so patching `config.output_dir`
    would have no effect.
    """

    def fake_output_dir() -> Path:
        d = tmp_path / ".ppsspp-dfx" / "output"
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr("ppsspp_dfx_mcp.tools._common.output_dir", fake_output_dir)
    return tmp_path / ".ppsspp-dfx" / "output"


def _patch_capture(monkeypatch: pytest.MonkeyPatch, **returns: Any) -> AsyncMock:
    """Patch `session_capture` with a fake CaptureService.

    Keyword names are CaptureService method names; values are return values
    (bytes) or exceptions to raise.
    """
    capture = AsyncMock()
    for method_name, value in returns.items():
        if isinstance(value, BaseException):
            getattr(capture, method_name).side_effect = value
        else:
            getattr(capture, method_name).return_value = value

    @asynccontextmanager
    async def fake_session_capture(session_id: str) -> AsyncIterator[Any]:
        yield AsyncMock(), capture

    monkeypatch.setattr(sc, "session_capture", fake_session_capture)
    return capture


def _image_blocks(result: CallToolResult) -> list[ImageContent]:
    return [b for b in result.content if isinstance(b, ImageContent)]


class TestScreenshotPersistence:
    """`ppsspp_screenshot` auto-save contract."""

    async def test_render_capture_writes_file_and_reports_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        out_root = _patch_output_dir(monkeypatch, tmp_path)
        _patch_capture(monkeypatch, screenshot=_PNG)

        result = await sc.screenshot(session_id="s1", source="render")

        meta = result.structured_content
        assert meta is not None
        # Scenario: screenshot saved to output directory.
        saved = Path(meta["file_path"])
        assert saved.is_file()
        assert saved.read_bytes() == _PNG
        # Scenario: output directory auto-created (the subdir did not exist
        # before the call — _patch_output_dir only creates the root).
        assert saved.parent == out_root / "screenshots"
        assert meta["size_bytes"] == len(_PNG)
        assert meta["width"] == 64
        assert meta["height"] == 32
        assert meta["format"] == "png"
        assert meta["empty"] is False
        # Pixels ride in `content`; metadata rides in `structuredContent`.
        assert len(_image_blocks(result)) == 1
        assert "image_base64" not in meta

    async def test_vram_fallback_uses_its_own_label_in_filename(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The saved filename records the mode that actually produced it."""
        _patch_output_dir(monkeypatch, tmp_path)
        _patch_capture(monkeypatch, screenshot=b"", safe_screenshot=_PNG)

        result = await sc.screenshot(session_id="s1", source="render")

        meta = result.structured_content
        assert meta is not None
        assert meta["mode"] == "render→vram_fallback"
        assert "vram_fallback" in Path(meta["file_path"]).name
        assert Path(meta["file_path"]).is_file()

    async def test_jpeg_capture_uses_jpg_extension(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_output_dir(monkeypatch, tmp_path)
        jpeg = b"\xff\xd8\xff" + b"\x00" * 30
        _patch_capture(monkeypatch, _print_window_screenshot=jpeg)

        result = await sc.screenshot(session_id="s1", mode="printwindow")

        meta = result.structured_content
        assert meta is not None
        assert meta["format"] == "jpeg"
        assert Path(meta["file_path"]).suffix == ".jpg"
        assert _image_blocks(result)[0].mime_type == "image/jpeg"

    async def test_empty_capture_saves_nothing_and_clears_file_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Failure path: metadata only, no ImageContent, no file on disk."""
        out_root = _patch_output_dir(monkeypatch, tmp_path)
        _patch_capture(monkeypatch, screenshot=b"", safe_screenshot=b"")

        result = await sc.screenshot(session_id="s1", source="render")

        meta = result.structured_content
        assert meta is not None
        assert meta["file_path"] == ""
        assert meta["size_bytes"] == 0
        assert meta["empty"] is True
        assert _image_blocks(result) == []
        assert not (out_root / "screenshots").exists()


class TestTextureAndClutPersistence:
    """`ppsspp_dump_texture` / `ppsspp_dump_clut` auto-save contract."""

    async def test_texture_dump_writes_file_and_reports_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        out_root = _patch_output_dir(monkeypatch, tmp_path)
        _patch_capture(monkeypatch, dump_texture=_PNG)

        result = await sc.dump_texture(session_id="s1", level=2)

        meta = result.structured_content
        assert meta is not None
        saved = Path(meta["file_path"])
        assert saved.is_file()
        assert saved.read_bytes() == _PNG
        assert saved.parent == out_root / "textures"
        # Filename encodes the mipmap level so dumps of one session do not
        # collide into an indistinguishable pile.
        assert "level2" in saved.name
        assert meta["level"] == 2
        assert meta["size_bytes"] == len(_PNG)
        assert meta["format"] == "png"
        assert len(_image_blocks(result)) == 1

    async def test_clut_dump_writes_file_and_reports_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        out_root = _patch_output_dir(monkeypatch, tmp_path)
        _patch_capture(monkeypatch, dump_clut=_PNG)

        result = await sc.dump_clut(session_id="s1")

        meta = result.structured_content
        assert meta is not None
        saved = Path(meta["file_path"])
        assert saved.is_file()
        assert saved.read_bytes() == _PNG
        assert saved.parent == out_root / "cluts"
        assert saved.name.startswith("clut_")
        assert meta["size_bytes"] == len(_PNG)
        assert "image_base64" not in meta
        assert len(_image_blocks(result)) == 1
