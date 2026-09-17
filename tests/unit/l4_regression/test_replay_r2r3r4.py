"""L4 regression tests for replay R2/R3/R4 fixes.

its R0 live verification (2026-09-12: boot-aligned replay of the TOPX S6a
macro reached the naming-menu endpoint; executing=True survives game reset).

Locks in:
- parse_replay_blob_b64: packed 17-byte item walk with MASK_SIDEDATA
  payload skipping, span extraction, and the corrupt-blob error paths
- execute/load responses carry t0_s / estimated_end_s / event_count +
  the boot-aligned sequence hint (R3)
- execute/load auto-abort a live executing/saving replay first (R4)
- restore_rtc defaults to False; True restores even base_rtc=0 (R2,
  including the old `and ppr.base_rtc` edge bug)
"""

from __future__ import annotations

import base64
import json
import struct
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.models.replay import parse_replay_blob_b64
from ppsspp_dfx_mcp.tools import replay as replay_module
from ppsspp_dfx_mcp.tools._common import resolve_output_path
from ppsspp_dfx_mcp.tools.replay import replay as replay_tool

# ============================================================================
# Blob construction helpers (ReplayItemHeader: pack(1), 17 bytes)
# ============================================================================


def _item(action: int, ts_us: int, u32: int = 0, payload: bytes = b"") -> bytes:
    # union storage is 8 bytes (u64_le result64 is the largest member);
    # a u32 write leaves the upper half zero in freshly built items.
    head = struct.pack("<BQQ", action, ts_us, u32)
    assert len(head) == 17
    return head + payload


def _b64(*chunks: bytes) -> str:
    return base64.b64encode(b"".join(chunks)).decode("ascii")


BUTTONS, ANALOG = 0, 1
MASK_SIDEDATA = 0x80


# ============================================================================
# parse_replay_blob_b64 (R3)
# ============================================================================


class TestParseReplayBlob:
    def test_walk_with_sidedata(self):
        blob = _b64(
            _item(BUTTONS, 41_390_000, u32=0x8),
            _item(ANALOG, 41_400_000, u32=0x80808080),
            _item(MASK_SIDEDATA | 10, 41_500_000, u32=4, payload=b"DATA"),
            _item(BUTTONS, 77_400_000, u32=0x0),
        )
        span = parse_replay_blob_b64(blob)
        assert span.event_count == 4
        assert span.t0_s == pytest.approx(41.39)
        assert span.end_s == pytest.approx(77.40)

    def test_empty_blob_rejected(self):
        with pytest.raises(ValueError, match="empty"):
            parse_replay_blob_b64("")

    def test_invalid_base64_rejected(self):
        with pytest.raises(ValueError, match="base64"):
            parse_replay_blob_b64("!!!not-base64!!!")

    def test_truncated_header_rejected(self):
        blob = base64.b64encode(b"x" * 10).decode("ascii")
        with pytest.raises(ValueError, match="truncated"):
            parse_replay_blob_b64(blob)

    def test_corrupt_sidedata_length_rejected(self):
        # size claims 100 payload bytes but the blob ends right after
        blob = base64.b64encode(_item(MASK_SIDEDATA | 10, 1000, u32=100, payload=b"ab")).decode(
            "ascii"
        )
        with pytest.raises(ValueError, match="corrupt"):
            parse_replay_blob_b64(blob)


# ============================================================================
# Tool wiring (R3 span fields + R4 auto-abort + R2 restore_rtc)
# ============================================================================


def _make_client(saving: bool = False, executing: bool = False) -> AsyncMock:
    mock = AsyncMock()
    mock.replay_status.return_value = {
        "executing": executing,
        "saving": saving,
    }
    # transport.call always yields dicts; the execute branch unpacks it.
    mock.replay_execute.return_value = {}
    mock.replay_abort.return_value = {}
    mock.replay_time_set.return_value = {}
    return mock


def _patch_session_client(monkeypatch: pytest.MonkeyPatch, mock: AsyncMock) -> None:
    @asynccontextmanager
    async def fake(session_id: str) -> AsyncIterator[AsyncMock]:
        yield mock

    monkeypatch.setattr(replay_module, "session_client", fake)


def _isolate_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ppsspp_dfx_mcp.tools import _common as common

    (tmp_path / "replays").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, "output_dir", lambda: tmp_path)


def _write_ppr(tmp_path: Path, name: str, base64_data: str, base_rtc: int = 0) -> str:
    ppr_path = resolve_output_path("replays", name)
    ppr_path.write_text(
        json.dumps(
            {
                "ppr_format_version": 1,
                "version": 1,
                "base64": base64_data,
                "base_rtc": base_rtc,
                "recorded_at": 0.0,
                "session_note": "",
            }
        ),
        encoding="utf-8",
    )
    return name


class TestExecuteLoadResponses:
    async def test_execute_carries_span_and_sequence(self, monkeypatch: pytest.MonkeyPatch):
        mock = _make_client()
        _patch_session_client(monkeypatch, mock)
        b64 = _b64(_item(BUTTONS, 5_000_000, u32=0x8))
        res = await replay_tool(session_id="s1", action="execute", version=1, base64_input=b64)
        data = res["data"]
        assert data["t0_s"] == 5.0
        assert data["estimated_end_s"] == 5.0
        assert data["event_count"] == 1
        assert "reset" in data["boot_aligned_sequence"]
        mock.replay_execute.assert_awaited_once()

    async def test_load_carries_span(self, tmp_path, monkeypatch):
        mock = _make_client()
        _patch_session_client(monkeypatch, mock)
        _isolate_output_dir(tmp_path, monkeypatch)
        b64 = _b64(
            _item(BUTTONS, 1_000_000, u32=0x8),
            _item(BUTTONS, 9_000_000, u32=0x0),
        )
        name = _write_ppr(tmp_path, "span.ppr", b64)
        res = await replay_tool(session_id="s1", action="load", file_path=name)
        data = res["data"]
        assert data["t0_s"] == 1.0
        assert data["estimated_end_s"] == 9.0
        assert data["restore_rtc_applied"] is False


class TestAutoAbort:
    async def test_execute_aborts_busy_replay_first(self, monkeypatch: pytest.MonkeyPatch):
        mock = _make_client(executing=True)
        _patch_session_client(monkeypatch, mock)
        b64 = _b64(_item(BUTTONS, 1_000_000, u32=0x8))
        await replay_tool(session_id="s1", action="execute", version=1, base64_input=b64)
        mock.replay_abort.assert_awaited_once()
        mock.replay_execute.assert_awaited_once()

    async def test_load_aborts_saving_replay_first(self, tmp_path, monkeypatch):
        mock = _make_client(saving=True)
        _patch_session_client(monkeypatch, mock)
        _isolate_output_dir(tmp_path, monkeypatch)
        name = _write_ppr(tmp_path, "busy.ppr", _b64(_item(BUTTONS, 1_000_000, u32=0x8)))
        await replay_tool(session_id="s1", action="load", file_path=name)
        mock.replay_abort.assert_awaited_once()

    async def test_idle_replay_not_aborted(self, monkeypatch: pytest.MonkeyPatch):
        mock = _make_client()
        _patch_session_client(monkeypatch, mock)
        b64 = _b64(_item(BUTTONS, 1_000_000, u32=0x8))
        await replay_tool(session_id="s1", action="execute", version=1, base64_input=b64)
        mock.replay_abort.assert_not_awaited()


class TestRestoreRtc:
    async def test_default_off_does_not_touch_clock(self, tmp_path, monkeypatch):
        mock = _make_client()
        _patch_session_client(monkeypatch, mock)
        _isolate_output_dir(tmp_path, monkeypatch)
        name = _write_ppr(
            tmp_path,
            "nortc.ppr",
            _b64(_item(BUTTONS, 1_000_000, u32=0x8)),
            base_rtc=1_788_891_921,
        )
        res = await replay_tool(session_id="s1", action="load", file_path=name)
        mock.replay_time_set.assert_not_awaited()
        assert res["data"]["restore_rtc_applied"] is False

    async def test_true_restores_even_zero_base_rtc(self, tmp_path, monkeypatch):
        """R2 edge fix: the old `restore_rtc and ppr.base_rtc` guard
        silently skipped a legal epoch-0 base."""
        mock = _make_client()
        _patch_session_client(monkeypatch, mock)
        _isolate_output_dir(tmp_path, monkeypatch)
        name = _write_ppr(
            tmp_path,
            "epoch0.ppr",
            _b64(_item(BUTTONS, 1_000_000, u32=0x8)),
            base_rtc=0,
        )
        res = await replay_tool(session_id="s1", action="load", file_path=name, restore_rtc=True)
        mock.replay_time_set.assert_awaited_once_with(value=0)
        assert res["data"]["restore_rtc_applied"] is True
