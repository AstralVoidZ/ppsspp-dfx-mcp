"""L3 orchestration tests: state sync + docs (batch 6).

Anchors:
- D-16: ws_connected updated to True after WS connect, False on disconnect
- D-04/D-29: screenshot render fails → vram fallback
- D-28: _image_dims parses PNG + JPEG headers
- D-09: evaluate description mentions no dereference support

L3 focus: Session.with_ws_connected + session_manager.update_ws_connected
+ screenshot fallback + image dimension parsing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest

from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.tools.screenshot import _image_dims, _jpeg_dims


# Isolate session store: save/restore around each test so failures
# don't leak state to subsequent tests.
@pytest.fixture(autouse=True)
def _isolate_session_store():
    from ppsspp_dfx_mcp.session import session_manager as sm
    original = sm._load_sessions()
    yield
    sm._save_sessions(original)


# ============================================================================
# D-16: Session.with_ws_connected
# ============================================================================


class TestSessionWsConnected:
    """L3: Session.with_ws_connected updates ws_connected flag (D-16)."""

    def test_with_ws_connected_true(self) -> None:
        """with_ws_connected(True) returns new Session with ws_connected=True."""
        sess = Session(
            session_id="sess-1",
            iso_path="/test.iso",
            pid=123,
            ws_url="ws://127.0.0.1:12345/debugger",
            ws_connected=False,
        )
        updated = sess.with_ws_connected(True)

        assert updated.ws_connected is True
        # Original session is NOT mutated (frozen dataclass).
        assert sess.ws_connected is False
        # Other fields preserved.
        assert updated.session_id == sess.session_id
        assert updated.pid == sess.pid
        assert updated.exec_count == sess.exec_count

    def test_with_ws_connected_false(self) -> None:
        """with_ws_connected(False) returns new Session with ws_connected=False."""
        sess = Session(
            session_id="sess-1",
            iso_path="/test.iso",
            pid=123,
            ws_url="ws://127.0.0.1:12345/debugger",
            ws_connected=True,
        )
        updated = sess.with_ws_connected(False)

        assert updated.ws_connected is False

    def test_extra_dict_copied_not_shared(self) -> None:
        """with_ws_connected copies extra dict (not shared reference)."""
        sess = Session(
            session_id="sess-1",
            iso_path="/test.iso",
            extra={"key": "value"},
        )
        updated = sess.with_ws_connected(True)

        assert updated.extra == sess.extra
        assert updated.extra is not sess.extra  # Different dict objects.


# ============================================================================
# D-16: session_manager.update_ws_connected
# ============================================================================


class TestUpdateWsConnected:
    """L3: session_manager.update_ws_connected persists ws_connected (D-16)."""

    @pytest.mark.asyncio
    async def test_update_ws_connected_persists(self) -> None:
        """update_ws_connected(True) writes ws_connected=True to disk."""
        from ppsspp_dfx_mcp.session import session_manager as sm

        # Seed with a disconnected session.
        sess = Session(
            session_id="sess-1",
            iso_path="/test.iso",
            pid=123,
            ws_url="ws://127.0.0.1:12345/debugger",
            ws_connected=False,
        )
        sm._save_sessions({"sess-1": sess})

        mgr = sm.SessionManager()
        await mgr.update_ws_connected("sess-1", True)

        loaded = sm._load_sessions()
        assert loaded["sess-1"].ws_connected is True, (
            "D-16: update_ws_connected(True) must persist ws_connected=True."
        )

    @pytest.mark.asyncio
    async def test_update_ws_connected_missing_session(self) -> None:
        """update_ws_connected on missing session is a no-op."""
        from ppsspp_dfx_mcp.session import session_manager as sm

        sm._save_sessions({})

        mgr = sm.SessionManager()
        # Should not raise.
        assert await mgr.update_ws_connected("nonexistent", True) is None  # no-op on missing session


# ============================================================================
# D-16: client_helper updates ws_connected on connect/disconnect
# ============================================================================


class TestClientHelperWsConnected:
    """L3: per-call ws_connected persistence is GONE (W6 fix, 2026-09-06).

    Contract change: session_client_with_transport no longer writes
    ws_connected=True on entry / False on exit. That D-16 mechanism cost
    4 extra sessions.json file ops per tool call and oscillated the
    persisted flag under concurrent calls; since F-8,
    get_session_state reports transport.is_connected() live, and
    touch_session folds that live value into the persisted record.
    These tests pin the new contract.
    """

    @pytest.mark.asyncio
    async def test_fallback_call_does_not_oscillate_persisted_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fallback-path tool call leaves the persisted flag untouched."""
        from ppsspp_dfx_mcp.session import client_helper, session_manager as sm

        sess = Session(
            session_id="sess-1",
            iso_path="/test.iso",
            pid=None,  # pid=None avoids liveness check in get_session_state.
            ws_url="ws://127.0.0.1:12345/debugger",
            ws_connected=False,
        )
        sm._save_sessions({"sess-1": sess})

        mock_transport = AsyncMock()
        mock_transport.connect = AsyncMock()
        mock_transport.send_version = AsyncMock()
        mock_transport.close = AsyncMock()

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.session.client_helper.WsTransport",
            lambda *a, **kw: mock_transport,
        )

        async with client_helper.session_client("sess-1"):
            # W6: inside the context the persisted flag must NOT be
            # flipped to True anymore (no per-call write).
            loaded = sm._load_sessions()
            assert loaded["sess-1"].ws_connected is False, (
                "W6: session_client must not persist ws_connected per call."
            )

        # After exit: still untouched (no False write either).
        loaded = sm._load_sessions()
        assert loaded["sess-1"].ws_connected is False

    @pytest.mark.asyncio
    async def test_touch_session_folds_live_transport_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """W6: touch_session persists transport.is_connected() when a
        session-level transport exists."""
        from ppsspp_dfx_mcp.session import session_manager as sm
        from unittest.mock import MagicMock

        sess = Session(
            session_id="sess-1",
            iso_path="/test.iso",
            pid=None,
            ws_url="ws://127.0.0.1:12345/debugger",
            ws_connected=False,
        )
        sm._save_sessions({"sess-1": sess})

        mgr = sm.SessionManager()
        mock_transport = MagicMock()
        mock_transport.is_connected.return_value = True
        mgr._transports["sess-1"] = mock_transport

        await mgr.touch_session("sess-1")
        loaded = sm._load_sessions()
        assert loaded["sess-1"].ws_connected is True, (
            "W6: touch_session must fold the live transport state in."
        )

        # And the flag follows the real connection state, not a toggle.
        mock_transport.is_connected.return_value = False
        await mgr.touch_session("sess-1")
        loaded = sm._load_sessions()
        assert loaded["sess-1"].ws_connected is False


# ============================================================================
# D-04/D-29: screenshot render → vram fallback
# ============================================================================


class TestScreenshotRenderFallback:
    """L3: screenshot(render) falls back to safe_screenshot when empty (D-04/D-29).

    When render_color returns empty bytes (e.g., title screen not rendered),
    the tool calls safe_screenshot (vram path) as fallback.
    """

    @pytest.mark.asyncio
    async def test_render_empty_triggers_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """render returns empty → safe_screenshot called → non-empty result."""
        from ppsspp_dfx_mcp.tools import screenshot as sc

        mock_capture = AsyncMock()
        # render returns empty.
        mock_capture.screenshot.return_value = b""
        # safe_screenshot returns valid PNG.
        mock_capture.safe_screenshot.return_value = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20

        @asynccontextmanager
        async def fake_session_capture(session_id: str):
            yield AsyncMock(), mock_capture

        monkeypatch.setattr(sc, "session_capture", fake_session_capture)

        result = await sc.screenshot(
            session_id="sess-1", source="render"
        )

        # safe_screenshot was called as fallback.
        mock_capture.safe_screenshot.assert_awaited_once()
        # Metadata shows fallback label. BREAKING (task 4.2): the tool now
        # returns CallToolResult — metadata moved from content[0] (a JSON
        # text block) to structured_content; the pixels live in content.
        meta = result.structured_content
        assert meta is not None
        assert "fallback" in meta["mode"].lower() or meta["mode"] == "render→vram_fallback"


# ============================================================================
# D-28: _image_dims parses PNG + JPEG
# ============================================================================


class TestImageDims:
    """L3: _image_dims extracts dimensions from PNG and JPEG (D-28)."""

    def test_png_dims_extracted(self) -> None:
        """PNG IHDR width/height extracted correctly."""
        # Minimal PNG: 8-byte sig + IHDR (13 bytes data).
        # Width=480 (0x01E0), Height=272 (0x0110).
        png = b"\x89PNG\r\n\x1a\n"
        png += b"\x00\x00\x00\x0D"  # IHDR length
        png += b"IHDR"  # chunk type
        png += (480).to_bytes(4, "big")  # width
        png += (272).to_bytes(4, "big")  # height
        png += b"\x08\x06\x00\x00\x00"  # bit depth, color type, etc.
        png += b"\x00" * 4  # CRC

        w, h = _image_dims(png)
        assert w == 480
        assert h == 272

    def test_jpeg_dims_extracted(self) -> None:
        """JPEG SOF0 marker width/height extracted correctly."""
        # Minimal JPEG with SOF0 marker.
        jpg = b"\xff\xd8"  # SOI
        jpg += b"\xff\xe0"  # APP0 marker
        jpg += b"\x00\x10"  # length=16
        jpg += b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        jpg += b"\xff\xc0"  # SOF0 marker
        jpg += b"\x00\x11"  # length=17
        jpg += b"\x08"  # precision
        jpg += (272).to_bytes(2, "big")  # height
        jpg += (480).to_bytes(2, "big")  # width
        jpg += b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"  # components

        w, h = _image_dims(jpg)
        assert w == 480
        assert h == 272

    def test_empty_data_returns_zero(self) -> None:
        """Empty bytes → (0, 0)."""
        assert _image_dims(b"") == (0, 0)

    def test_invalid_data_returns_zero(self) -> None:
        """Invalid data → (0, 0)."""
        assert _image_dims(b"not an image") == (0, 0)

    def test_short_png_returns_zero(self) -> None:
        """Truncated PNG → (0, 0)."""
        assert _image_dims(b"\x89PNG\r\n\x1a\n") == (0, 0)


# ============================================================================
# D-09: evaluate description mentions no dereference
# ============================================================================


class TestEvaluateDescription:
    """L3: evaluate expression description mentions no dereference (D-09)."""

    def test_description_mentions_no_dereference(self) -> None:
        """Field description says PPSSPP does NOT support dereference."""
        import typing

        from ppsspp_dfx_mcp.tools.evaluate import evaluate

        # Check the `expression` parameter's Field description (the
        # user-facing contract) rather than the source code text —
        # brittle to refactors that preserve behavior.
        hints = typing.get_type_hints(evaluate, include_extras=True)
        expr_meta = hints.get("expression")
        desc = ""
        if expr_meta is not None and hasattr(expr_meta, "__metadata__"):
            for meta in expr_meta.__metadata__:
                if hasattr(meta, "description"):
                    desc = meta.description or ""
                    break
        assert "dereference" in desc.lower() or "read_u32" in desc.lower(), (
            "D-09: evaluate expression field description must mention that "
            "dereference is NOT supported, and suggest read_u32 instead."
        )
