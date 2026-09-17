"""L3 orchestration tests: WS protocol fidelity (source-verified behaviours).

Anchor: PPSSPP WebSocket source (open_source/ppsspp/Core/Debugger/WebSocket/),
cross-checked event-by-event against this codebase on 2026-09-06. This file
locks the behaviours where a naive implementation diverges from the actual
PPSSPP semantics:

- hold_buttons(""): PPSSPP's input.buttons.send only SETS the keys present
  in the buttons object (InputSubscriber.cpp:132-154) — an empty object is
  a no-op. The documented "release all" behaviour must send false for every
  button in the buttonLookup table.
- press_button: input.buttons.press is asynchronous — the ticket echoes
  after `duration` frames (InputSubscriber.cpp:170-192), so the transport
  timeout must scale with duration (long presses otherwise time out).
- breakpoint(mem_remove): PPSSPP matches memchecks by exact start+end pair
  (BreakpointSubscriber.cpp:406-437) — removing with a size that differs
  from the recorded one silently fails. The tool resolves the actual size
  from memory.breakpoint.list first.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.errors import BreakpointError
from ppsspp_dfx_mcp.service.debug_client import _press_button_timeout
from ppsspp_dfx_mcp.tools.breakpoint import breakpoint
from ppsspp_dfx_mcp.tools.input import _PPSSPP_ALL_BUTTONS, hold_buttons
from ppsspp_dfx_mcp.tools.input import press_button as press_button_tool


def _fake_session_client(tool_module: str, mock_client: AsyncMock):
    @asynccontextmanager
    async def _cm(session_id: str) -> AsyncIterator[AsyncMock]:
        yield mock_client

    return _cm


# ============================================================================
# hold_buttons: empty string = release all (send false for every button)
# ============================================================================


class TestHoldButtonsReleaseAll:
    """hold_buttons("") must release every button, not send an empty dict."""

    @pytest.mark.asyncio
    async def test_empty_string_sends_false_for_all_buttons(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client = AsyncMock()
        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.input.session_client",
            _fake_session_client("input", mock_client),
        )

        await hold_buttons(session_id="sess-1", buttons="")

        mock_client.hold_buttons.assert_awaited_once()
        sent = mock_client.hold_buttons.await_args.kwargs["buttons"]
        assert set(sent.keys()) == set(_PPSSPP_ALL_BUTTONS), (
            "release-all must enumerate every PPSSPP buttonLookup name"
        )
        assert all(v is False for v in sent.values()), "release-all must set every button to False"

    @pytest.mark.asyncio
    async def test_named_buttons_send_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_client = AsyncMock()
        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.input.session_client",
            _fake_session_client("input", mock_client),
        )

        await hold_buttons(session_id="sess-1", buttons="cross|ltrigger")

        sent = mock_client.hold_buttons.await_args.kwargs["buttons"]
        assert sent == {"cross": True, "ltrigger": True}

    def test_button_table_matches_ppsspp_button_lookup(self) -> None:
        """The release-all table must cover exactly the PPSSPP buttonLookup
        names (InputSubscriber.cpp:27-53) — no more, no fewer."""
        expected = {
            "cross",
            "circle",
            "triangle",
            "square",
            "up",
            "down",
            "left",
            "right",
            "start",
            "select",
            "home",
            "screen",
            "note",
            "ltrigger",
            "rtrigger",
            "hold",
            "wlan",
            "remote_hold",
            "vol_up",
            "vol_down",
            "disc",
            "memstick",
            "forward",
            "back",
            "playpause",
        }
        assert set(_PPSSPP_ALL_BUTTONS) == expected


# ============================================================================
# press_button: timeout scales with press duration
# ============================================================================


class TestPressButtonTimeout:
    """The ticket for input.buttons.press echoes after `duration` frames."""

    def test_short_press_uses_default_floor(self) -> None:
        assert _press_button_timeout(1) == 5.0
        assert _press_button_timeout(10) == 5.0

    def test_long_press_scales(self) -> None:
        # 600 frames = 10 s at 60 FPS → 10 * 1.5 + 2 = 17 s.
        assert _press_button_timeout(600) == pytest.approx(17.0)
        # 1200 frames = 20 s → 32 s.
        assert _press_button_timeout(1200) == pytest.approx(32.0)

    @pytest.mark.asyncio
    async def test_tool_passes_duration_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_client = AsyncMock()
        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.input.session_client",
            _fake_session_client("input", mock_client),
        )

        await press_button_tool(session_id="sess-1", button="cross", duration=600)

        mock_client.press_button.assert_awaited_once_with(button="cross", duration=600)


# ============================================================================
# breakpoint(mem_remove): resolve the actual memcheck size before removing
# ============================================================================


def _mock_breakpoint_session(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock_client = AsyncMock()

    @asynccontextmanager
    async def _cm(session_id: str) -> AsyncIterator[AsyncMock]:
        yield mock_client

    monkeypatch.setattr("ppsspp_dfx_mcp.tools.breakpoint.session_client", _cm)
    return mock_client


class TestMemRemoveResolvesActualSize:
    """mem_remove must remove with the memcheck's recorded size."""

    @pytest.mark.asyncio
    async def test_uses_recorded_size_from_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_client = _mock_breakpoint_session(monkeypatch)
        # A 16-byte watch exists at the address; the tool is called with the
        # default size=4, which would not match on the PPSSPP side.
        mock_client.mem_bp_list.return_value = {
            "breakpoints": [
                {
                    "address": 0x08A0D000,
                    "size": 16,
                    "read": True,
                    "write": True,
                    "change": False,
                    "enabled": True,
                    "log": False,
                    "hits": 0,
                    "condition": None,
                    "logFormat": None,
                    "symbol": None,
                },
            ],
        }

        await breakpoint(
            session_id="sess-1",
            action="mem_remove",
            address="0x08A0D000",  # size defaults to 4
        )

        remove_kwargs = mock_client.mem_bp_remove.await_args.kwargs
        assert remove_kwargs == {"address": 0x08A0D000, "size": 16}, (
            "mem_remove must send the memcheck's recorded size, not the caller default"
        )

    @pytest.mark.asyncio
    async def test_missing_breakpoint_fails_loudly_on_default_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_client = _mock_breakpoint_session(monkeypatch)
        mock_client.mem_bp_list.return_value = {"breakpoints": []}

        with pytest.raises(BreakpointError, match="no memory breakpoint"):
            await breakpoint(
                session_id="sess-1",
                action="mem_remove",
                address="0x08A0D000",
            )
        mock_client.mem_bp_remove.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_nondefault_size_missing_breakpoint_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """S3 fix (2026-09-06): the old carve-out that sent a remove with an
        explicitly-passed non-default size (silently succeeding against
        nothing) is gone — missing memcheck + any size = BreakpointError."""
        mock_client = _mock_breakpoint_session(monkeypatch)
        mock_client.mem_bp_list.return_value = {"breakpoints": []}

        with pytest.raises(BreakpointError, match="no memory breakpoint"):
            await breakpoint(
                session_id="sess-1",
                action="mem_remove",
                address="0x08A0D000",
                size=16,
            )
        mock_client.mem_bp_remove.assert_not_awaited()
