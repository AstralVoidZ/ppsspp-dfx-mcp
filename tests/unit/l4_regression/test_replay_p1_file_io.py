"""L4 regression tests for P1 replay file I/O (save / load / .ppr format).

Locks in:
- PPRFile.to_dict / from_dict round-trip (data layer)
- PPRFile.from_dict validation (missing fields, version mismatch)
- save action composes flush + time_get + file write (tool layer)
- load action composes file read + optional time_set + execute (tool layer)
- save → load end-to-end round-trip consistency
- error handling (missing file, invalid JSON, invalid schema)

P1 (OpenSpec change `add-replay-tools`). Uses monkeypatch to mock
session_client, returning an AsyncMock that records replay_flush /
replay_time_get / replay_time_set / replay_execute calls.
"""

from __future__ import annotations

import base64 as _b64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.errors import ToolError
from ppsspp_dfx_mcp.models.replay import PPRFile
from ppsspp_dfx_mcp.tools._common import resolve_output_path
from ppsspp_dfx_mcp.tools.replay import replay

# ============================================================================
# PPRFile data layer — to_dict / from_dict round-trip
# ============================================================================


class TestPPRFileRoundTrip:
    """PPRFile.to_dict → from_dict preserves all fields."""

    def test_round_trip_preserves_all_fields(self):
        """R-10: to_dict → from_dict preserves all fields."""
        original = PPRFile(
            version=1,
            base64="AAEC",
            base_rtc=1785051771,
            recorded_at=1785051799.18,
            session_note="test recording",
        )
        restored = PPRFile.from_dict(original.to_dict())
        assert restored == original

    def test_round_trip_with_empty_note(self):
        """R-11: empty session_note round-trips correctly."""
        original = PPRFile(
            version=1,
            base64="AAEC",
            base_rtc=100,
            recorded_at=0.0,
            session_note="",
        )
        restored = PPRFile.from_dict(original.to_dict())
        assert restored.session_note == ""
        assert restored == original

    def test_ppr_format_version_is_1(self):
        """R-12: ppr_format_version is locked to 1 (schema stability)."""
        assert PPRFile().ppr_format_version == 1
        assert PPRFile(version=1, base64="x", base_rtc=0).ppr_format_version == 1

    def test_to_dict_includes_ppr_format_version(self):
        """R-13: to_dict output includes ppr_format_version field."""
        d = PPRFile(version=1, base64="x", base_rtc=0).to_dict()
        assert "ppr_format_version" in d
        assert d["ppr_format_version"] == 1


# ============================================================================
# PPRFile validation — from_dict rejects malformed input
# ============================================================================


class TestPPRFileValidation:
    """PPRFile.from_dict validates schema strictly."""

    def test_missing_ppr_format_version_raises(self):
        """R-14: missing ppr_format_version raises ValueError."""
        with pytest.raises(ValueError, match="missing 'ppr_format_version'"):
            PPRFile.from_dict({"version": 1, "base64": "x", "base_rtc": 0})

    def test_wrong_ppr_format_version_raises(self):
        """R-15: unsupported ppr_format_version raises ValueError."""
        with pytest.raises(ValueError, match="unsupported .ppr format version"):
            PPRFile.from_dict(
                {
                    "ppr_format_version": 99,
                    "version": 1,
                    "base64": "x",
                    "base_rtc": 0,
                }
            )

    def test_missing_version_raises(self):
        """R-16: missing 'version' raises ValueError."""
        with pytest.raises(ValueError, match="missing required field 'version'"):
            PPRFile.from_dict(
                {
                    "ppr_format_version": 1,
                    "base64": "x",
                    "base_rtc": 0,
                }
            )

    def test_missing_base64_raises(self):
        """R-17: missing 'base64' raises ValueError."""
        with pytest.raises(ValueError, match="missing required field 'base64'"):
            PPRFile.from_dict(
                {
                    "ppr_format_version": 1,
                    "version": 1,
                    "base_rtc": 0,
                }
            )

    def test_missing_base_rtc_raises(self):
        """R-18: missing 'base_rtc' raises ValueError."""
        with pytest.raises(ValueError, match="missing required field 'base_rtc'"):
            PPRFile.from_dict(
                {
                    "ppr_format_version": 1,
                    "version": 1,
                    "base64": "x",
                }
            )

    def test_optional_recorded_at_defaults_to_zero(self):
        """R-19: recorded_at defaults to 0.0 when absent."""
        ppr = PPRFile.from_dict(
            {
                "ppr_format_version": 1,
                "version": 1,
                "base64": "x",
                "base_rtc": 0,
            }
        )
        assert ppr.recorded_at == 0.0

    def test_optional_session_note_defaults_to_empty(self):
        """R-20: session_note defaults to '' when absent."""
        ppr = PPRFile.from_dict(
            {
                "ppr_format_version": 1,
                "version": 1,
                "base64": "x",
                "base_rtc": 0,
            }
        )
        assert ppr.session_note == ""


# ============================================================================
# save action — composes flush + time_get + file write
# ============================================================================


def _make_mock_client(
    flush_resp: dict | None = None,
    time_get_resp: dict | None = None,
    execute_resp: dict | None = None,
) -> AsyncMock:
    """Build an AsyncMock client with preset replay_* responses."""
    mock = AsyncMock()
    mock.replay_flush.return_value = flush_resp or {
        "version": 1,
        "base64": "AAEC",
    }
    mock.replay_time_get.return_value = time_get_resp or {"value": 1785051771}
    mock.replay_execute.return_value = execute_resp or {"ok": True}
    return mock


def _patch_session_client(monkeypatch: pytest.MonkeyPatch, mock_client: AsyncMock):
    """Patch tools.replay.session_client to yield mock_client."""

    @asynccontextmanager
    async def fake_session_client(session_id: str) -> AsyncIterator[AsyncMock]:
        yield mock_client

    monkeypatch.setattr(
        "ppsspp_dfx_mcp.tools.replay.session_client",
        fake_session_client,
    )


@pytest.fixture(autouse=True)
def _isolate_output_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """S2 contract: replay save/load resolve under output_dir()/replays/.

    Point the shared output_dir at tmp_path (pre-creating the replays
    directory) so tests never touch the real repo output tree.
    """
    import ppsspp_dfx_mcp.tools._common as common

    monkeypatch.setattr(common, "output_dir", lambda: tmp_path)
    (tmp_path / "replays").mkdir(parents=True, exist_ok=True)


class TestSaveAction:
    """save action composes flush + time_get + file write."""

    @pytest.mark.asyncio
    async def test_save_writes_ppr_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """R-21: save writes a valid .ppr file to file_path."""
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)

        ppr_path = resolve_output_path("replays", "rec.ppr")
        result = await replay(
            session_id="sess-1",
            action="save",
            file_path="rec.ppr",
            session_note="my recording",
        )

        # .ppr file exists and parses as JSON.
        assert ppr_path.is_file()
        raw = json.loads(ppr_path.read_text(encoding="utf-8"))
        ppr = PPRFile.from_dict(raw)

        assert ppr.version == 1
        assert ppr.base64 == "AAEC"
        assert ppr.base_rtc == 1785051771
        assert ppr.session_note == "my recording"
        assert ppr.ppr_format_version == 1

        # Tool result fields.
        assert result["action"] == "save"
        assert result["version"] == 1
        assert result["base64"] == "AAEC"
        assert result["base_rtc"] == 1785051771
        assert result["size"] == 3  # len(b64decode("AAEC")) == 3
        assert result["data"]["file_path"] == str(ppr_path.resolve())
        assert result["data"]["ppr_format_version"] == 1

    @pytest.mark.asyncio
    async def test_save_calls_flush_then_time_get(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """R-22: save calls replay_flush before replay_time_get."""
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)

        await replay(
            session_id="sess-1",
            action="save",
            file_path="x.ppr",
        )

        mock.replay_flush.assert_awaited_once()
        mock.replay_time_get.assert_awaited_once()
        # Order: flush must come before time_get.
        flush_call_order = (
            mock.method_calls.index(("replay_flush", (), {}))
            if ("replay_flush", (), {}) in mock.method_calls
            else -1
        )
        time_get_call_order = (
            mock.method_calls.index(("replay_time_get", (), {}))
            if ("replay_time_get", (), {}) in mock.method_calls
            else -1
        )
        # Both must be present and flush before time_get.
        assert flush_call_order >= 0
        assert time_get_call_order >= 0
        assert flush_call_order < time_get_call_order

    @pytest.mark.asyncio
    async def test_save_creates_replays_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """R-23 (S2 revision): save auto-creates output/replays/.

        The directory is server-controlled now — the caller cannot point
        save at an arbitrary nested path, so what is locked in is that a
        missing replays directory is created on first save.
        """
        import shutil

        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)

        shutil.rmtree(tmp_path / "replays")
        ppr_path = resolve_output_path("replays", "rec.ppr")
        await replay(
            session_id="sess-1",
            action="save",
            file_path="rec.ppr",
        )
        assert ppr_path.is_file()

    @pytest.mark.asyncio
    async def test_save_default_session_note_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """R-24: save without session_note writes empty note."""
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)

        ppr_path = resolve_output_path("replays", "rec.ppr")
        await replay(
            session_id="sess-1",
            action="save",
            file_path="rec.ppr",
        )
        ppr = PPRFile.from_dict(json.loads(ppr_path.read_text(encoding="utf-8")))
        assert ppr.session_note == ""

    @pytest.mark.asyncio
    async def test_save_computes_size_from_base64(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """R-25: save size field = len(b64decode(base64)), not raw len."""
        payload = "AAEC"  # decodes to 3 bytes
        mock = _make_mock_client(
            flush_resp={"version": 1, "base64": payload},
        )
        _patch_session_client(monkeypatch, mock)

        result = await replay(
            session_id="sess-1",
            action="save",
            file_path="rec.ppr",
        )
        assert result["size"] == 3
        assert result["size"] != len(payload)  # 3 != 4


# ============================================================================
# load action — composes file read + optional time_set + execute
# ============================================================================


class TestLoadAction:
    """load action composes file read + optional time_set + execute."""

    @pytest.mark.asyncio
    async def test_load_reads_file_and_executes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """R-26: load reads .ppr and calls replay_execute with stored data."""
        ppr_path = resolve_output_path("replays", "rec.ppr")
        ppr = PPRFile(
            version=1,
            base64="AAEC",
            base_rtc=1785051771,
            recorded_at=1785051799.0,
            session_note="x",
        )
        ppr_path.write_text(json.dumps(ppr.to_dict()), encoding="utf-8")

        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)

        result = await replay(
            session_id="sess-1",
            action="load",
            file_path="rec.ppr",
        )

        # execute called with stored version + base64.
        mock.replay_execute.assert_awaited_once_with(version=1, base64="AAEC")
        # R2 (2026-09-12): restore_rtc defaults False — the game-visible
        # clock of the running session must NOT be rewound implicitly.
        mock.replay_time_set.assert_not_awaited()

        # Result fields.
        assert result["action"] == "load"
        assert result["version"] == 1
        assert result["base64"] == "AAEC"
        assert result["base_rtc"] == 1785051771
        assert result["executing"] is True
        assert result["size"] == 3
        assert result["data"]["file_path"] == str(ppr_path.resolve())
        assert result["data"]["restore_rtc_applied"] is False

    @pytest.mark.asyncio
    async def test_load_with_restore_rtc_false_skips_time_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """R-27: restore_rtc=False skips replay_time_set call."""
        ppr_path = resolve_output_path("replays", "rec.ppr")
        ppr = PPRFile(version=1, base64="AAEC", base_rtc=1785051771)
        ppr_path.write_text(json.dumps(ppr.to_dict()), encoding="utf-8")

        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)

        result = await replay(
            session_id="sess-1",
            action="load",
            file_path="rec.ppr",
            restore_rtc=False,
        )

        mock.replay_execute.assert_awaited_once()
        mock.replay_time_set.assert_not_awaited()
        assert result["data"]["restore_rtc_applied"] is False

    @pytest.mark.asyncio
    async def test_load_with_zero_base_rtc_still_restores_when_explicit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """R-28 (R2 fix 2026-09-12): the old `restore_rtc and base_rtc`
        guard silently skipped a LEGAL epoch-0 base; explicit
        restore_rtc=True now restores even base_rtc=0."""
        ppr_path = resolve_output_path("replays", "rec.ppr")
        ppr = PPRFile(version=1, base64="AAEC", base_rtc=0)
        ppr_path.write_text(json.dumps(ppr.to_dict()), encoding="utf-8")

        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)

        result = await replay(
            session_id="sess-1",
            action="load",
            file_path="rec.ppr",
            restore_rtc=True,
        )
        mock.replay_time_set.assert_awaited_once_with(value=0)
        assert result["data"]["restore_rtc_applied"] is True


# ============================================================================
# save → load end-to-end round-trip
# ============================================================================


class TestSaveLoadRoundTrip:
    """save → load preserves recording data end-to-end."""

    @pytest.mark.asyncio
    async def test_save_then_load_preserves_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """R-29: save followed by load preserves version/base64/base_rtc."""
        # save uses this mock (flush + time_get).
        save_mock = _make_mock_client(
            flush_resp={"version": 1, "base64": "AAEC"},
            time_get_resp={"value": 1785051771},
        )
        _patch_session_client(monkeypatch, save_mock)

        _ppr_path = resolve_output_path("replays", "round_trip.ppr")
        save_result = await replay(
            session_id="sess-1",
            action="save",
            file_path="round_trip.ppr",
            session_note="round-trip test",
        )

        # load uses a fresh mock (execute).
        load_mock = _make_mock_client()
        _patch_session_client(monkeypatch, load_mock)

        load_result = await replay(
            session_id="sess-1",
            action="load",
            file_path="round_trip.ppr",
        )

        # Round-trip: save → load preserves version/base64/base_rtc.
        assert load_result["version"] == save_result["version"] == 1
        assert load_result["base64"] == save_result["base64"] == "AAEC"
        assert load_result["base_rtc"] == save_result["base_rtc"] == 1785051771
        assert load_result["size"] == save_result["size"] == 3

        # load's execute received save's flushed data verbatim.
        load_mock.replay_execute.assert_awaited_once_with(
            version=save_result["version"],
            base64=save_result["base64"],
        )

    @pytest.mark.asyncio
    async def test_round_trip_with_real_spike_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """R-30: round-trip works with realistic spike-length recording."""
        # 51-byte recording from spike U1 (base64-decoded length).
        spike_b64 = "ACycRgEAAAAAAEAAAAAAAAQBLJxGAQAAAACAgICAAAAABABExk8BAAAAAAAAAAB/AADQ"
        expected_size = len(_b64.b64decode(spike_b64))

        save_mock = _make_mock_client(
            flush_resp={"version": 1, "base64": spike_b64},
            time_get_resp={"value": 1785051771},
        )
        _patch_session_client(monkeypatch, save_mock)

        _ppr_path = resolve_output_path("replays", "spike.ppr")
        await replay(
            session_id="sess-1",
            action="save",
            file_path="spike.ppr",
            session_note="spike U1",
        )

        load_mock = _make_mock_client()
        _patch_session_client(monkeypatch, load_mock)

        result = await replay(
            session_id="sess-1",
            action="load",
            file_path="spike.ppr",
        )
        assert result["base64"] == spike_b64
        assert result["size"] == expected_size
        assert result["version"] == 1


# ============================================================================
# load error handling
# ============================================================================


class TestLoadErrors:
    """load action surfaces file/schema errors as ToolError."""

    @pytest.mark.asyncio
    async def test_load_missing_file_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """R-31: load non-existent file raises ToolError code=ARGS_INVALID."""
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)

        with pytest.raises(ToolError, match="\\.ppr file not found") as exc_info:
            await replay(
                session_id="sess-1",
                action="load",
                file_path="nope.ppr",
            )
        assert exc_info.value.code == "ARGS_INVALID"
        # execute must not be called when file is missing.
        mock.replay_execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_load_invalid_json_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """R-32: load invalid JSON raises ToolError with parse error."""
        bad = resolve_output_path("replays", "bad.ppr")
        bad.write_text("not json {{{", encoding="utf-8")
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)

        with pytest.raises(ToolError, match="JSON parse error") as exc_info:
            await replay(
                session_id="sess-1",
                action="load",
                file_path="bad.ppr",
            )
        assert exc_info.value.code == "ARGS_INVALID"

    @pytest.mark.asyncio
    async def test_load_invalid_schema_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """R-33: load valid JSON but missing ppr_format_version raises."""
        bad = resolve_output_path("replays", "bad.ppr")
        bad.write_text(
            json.dumps({"version": 1, "base64": "x", "base_rtc": 0}),
            encoding="utf-8",
        )
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)

        with pytest.raises(ToolError, match="invalid .ppr file") as exc_info:
            await replay(
                session_id="sess-1",
                action="load",
                file_path="bad.ppr",
            )
        assert exc_info.value.code == "ARGS_INVALID"
        mock.replay_execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_load_wrong_format_version_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """R-34: load .ppr with future format version raises."""
        bad = resolve_output_path("replays", "future.ppr")
        bad.write_text(
            json.dumps(
                {
                    "ppr_format_version": 99,
                    "version": 1,
                    "base64": "x",
                    "base_rtc": 0,
                }
            ),
            encoding="utf-8",
        )
        mock = _make_mock_client()
        _patch_session_client(monkeypatch, mock)

        with pytest.raises(ToolError, match="unsupported .ppr format version") as exc:
            await replay(
                session_id="sess-1",
                action="load",
                file_path="future.ppr",
            )
        assert exc.value.code == "ARGS_INVALID"


# ============================================================================
# S2 path containment + W5 save-failure recovery (2026-09-06 review fixes)
# ============================================================================


class TestS2PathContainment:
    """S2: save/load only accept bare file names inside output/replays/."""

    @pytest.mark.asyncio
    async def test_save_rejects_absolute_path(self, tmp_path: Path):
        with pytest.raises(ToolError, match="bare file name"):
            await replay(
                session_id="sess-1",
                action="save",
                file_path=str(tmp_path / "evil.ppr"),
            )

    @pytest.mark.asyncio
    async def test_save_rejects_traversal(self):
        with pytest.raises(ToolError, match="bare file name"):
            await replay(
                session_id="sess-1",
                action="save",
                file_path="../evil.ppr",
            )

    @pytest.mark.asyncio
    async def test_save_rejects_nested_path(self):
        with pytest.raises(ToolError, match="bare file name"):
            await replay(
                session_id="sess-1",
                action="save",
                file_path="sub/dir/rec.ppr",
            )

    @pytest.mark.asyncio
    async def test_load_rejects_absolute_path(self, tmp_path: Path):
        with pytest.raises(ToolError, match="bare file name"):
            await replay(
                session_id="sess-1",
                action="load",
                file_path=str(tmp_path / "evil.ppr"),
            )


class TestW5SaveFailureRecovery:
    """W5: when the post-flush write fails, the error carries version +
    base64 so the already-consumed recording is recoverable."""

    @pytest.mark.asyncio
    async def test_save_write_failure_rescues_payload_to_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import ppsspp_dfx_mcp.tools._common as common
        import ppsspp_dfx_mcp.tools.replay as replay_mod

        mock = _make_mock_client(flush_resp={"version": 7, "base64": "AAEC"})
        _patch_session_client(monkeypatch, mock)

        # Make the contained target unwritable: a DIRECTORY at the file path.
        (tmp_path / "replays" / "rec.ppr").mkdir()

        real_resolve = common.resolve_output_path

        def _still_contained(subdir: str, filename: str) -> Path:
            return real_resolve(subdir, filename)

        monkeypatch.setattr(replay_mod, "resolve_output_path", _still_contained)

        with pytest.raises(ToolError) as exc_info:
            await replay(
                session_id="sess-1",
                action="save",
                file_path="rec.ppr",
            )
        msg = str(exc_info.value)
        # W22 (review v2): the payload itself must NOT ride the error
        # text — a long recording made the error megabytes. It is
        # rescued to a server-generated .ppr next to the target instead.
        assert "failed to write .ppr file" in msg
        assert "AAEC" not in msg
        import json as _json

        rescued = list((tmp_path / "replays").glob("replay_rescue_*.ppr"))
        assert len(rescued) == 1, msg
        doc = _json.loads(rescued[0].read_text(encoding="utf-8"))
        assert doc["version"] == 7
        assert doc["base64"] == "AAEC"
        assert "replay_rescue_" in msg and "load" in msg
