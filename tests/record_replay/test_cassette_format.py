"""Tests for the cassette JSONL format (cassette.py).

Verifies:
- CassetteRecord serialization/deserialization roundtrip
- save_cassette / load_cassette roundtrip
- Partial corruption tolerance: malformed lines are skipped
- Line-level independent parsing
- All four record types (call / fire_and_forget / broadcast / state_change)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from record_replay.cassette import (
    CassetteRecord,
    load_cassette,
    save_cassette,
)

# ---------- CassetteRecord serialization ----------


class TestCassetteRecordSerialization:
    """CassetteRecord.to_json / from_json roundtrip for each record type."""

    def test_call_record_roundtrip(self):
        rec = CassetteRecord(
            type="call",
            event="memory.read_u32",
            params={"address": 143032320},
            response={"value": 1448497728},
            timestamp=1721800000.123,
        )
        line = rec.to_json()
        # Must be a single line (JSONL requirement).
        assert "\n" not in line

        restored = CassetteRecord.from_json(line)
        assert restored.type == "call"
        assert restored.event == "memory.read_u32"
        assert restored.params == {"address": 143032320}
        assert restored.response == {"value": 1448497728}
        assert restored.timestamp == 1721800000.123
        # Optional fields not set must be None.
        assert restored.message is None
        assert restored.state_delta is None

    def test_fire_and_forget_record_roundtrip(self):
        rec = CassetteRecord(
            type="fire_and_forget",
            event="cpu.stepping",
            params={"step": "into"},
            timestamp=1721800000.456,
        )
        line = rec.to_json()
        restored = CassetteRecord.from_json(line)
        assert restored.type == "fire_and_forget"
        assert restored.event == "cpu.stepping"
        assert restored.params == {"step": "into"}
        # No response for fire_and_forget.
        assert restored.response is None

    def test_broadcast_record_roundtrip(self):
        rec = CassetteRecord(
            type="broadcast",
            event="cpu.stepping",
            message={"event": "cpu.stepping", "stepping": True},
            timestamp=1721800000.789,
        )
        line = rec.to_json()
        restored = CassetteRecord.from_json(line)
        assert restored.type == "broadcast"
        assert restored.event == "cpu.stepping"
        assert restored.message == {"event": "cpu.stepping", "stepping": True}
        assert restored.response is None

    def test_state_change_record_roundtrip(self):
        rec = CassetteRecord(
            type="state_change",
            event="cpu.stepping",
            state_delta={"stepping": True},
            timestamp=1721800000.999,
        )
        line = rec.to_json()
        restored = CassetteRecord.from_json(line)
        assert restored.type == "state_change"
        assert restored.event == "cpu.stepping"
        assert restored.state_delta == {"stepping": True}

    def test_to_json_omits_none_optional_fields(self):
        """Optional fields (params/response/message/state_delta) must be
        omitted from the JSON when None, not serialized as null."""
        rec = CassetteRecord(
            type="fire_and_forget",
            event="cpu.stepping",
            timestamp=1.0,
        )
        payload = json.loads(rec.to_json())
        assert "params" not in payload
        assert "response" not in payload
        assert "message" not in payload
        assert "state_delta" not in payload

    def test_from_json_missing_timestamp_defaults_to_zero(self):
        """timestamp is optional on load — defaults to 0.0 if absent."""
        line = json.dumps({"type": "call", "event": "cpu.status"})
        rec = CassetteRecord.from_json(line)
        assert rec.timestamp == 0.0

    def test_from_json_missing_required_fields_raises_keyerror(self):
        """type and event are required — KeyError if missing."""
        line = json.dumps({"type": "call"})  # missing event
        with pytest.raises(KeyError):
            CassetteRecord.from_json(line)


# ---------- save_cassette / load_cassette roundtrip ----------


class TestCassetteFileRoundtrip:
    """save_cassette writes JSONL; load_cassette reads it back."""

    def test_save_load_roundtrip(self, tmp_path: Path):
        records = [
            CassetteRecord(
                type="call",
                event="memory.read_u32",
                params={"address": 0x08804000},
                response={"value": 1448497728},
                timestamp=1.0,
            ),
            CassetteRecord(
                type="fire_and_forget",
                event="cpu.stepping",
                params={"step": "into"},
                timestamp=2.0,
            ),
            CassetteRecord(
                type="broadcast",
                event="cpu.stepping",
                message={"stepping": True},
                timestamp=3.0,
            ),
            CassetteRecord(
                type="state_change",
                event="cpu.stepping",
                state_delta={"stepping": True},
                timestamp=4.0,
            ),
        ]
        cassette_path = tmp_path / "test.jsonl"
        save_cassette(records, cassette_path)

        # File must exist and parent dir created if needed.
        assert cassette_path.exists()

        # Each line must be valid JSON (JSONL invariant).
        lines = cassette_path.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 4
        for line in lines:
            json.loads(line)  # raises if invalid

        # Load back and verify.
        loaded = load_cassette(cassette_path)
        assert len(loaded) == 4
        assert loaded[0].type == "call"
        assert loaded[1].type == "fire_and_forget"
        assert loaded[2].type == "broadcast"
        assert loaded[3].type == "state_change"

    def test_save_creates_parent_directory(self, tmp_path: Path):
        """save_cassette must mkdir -p the parent if it doesn't exist."""
        cassette_path = tmp_path / "nested" / "deep" / "cassette.jsonl"
        save_cassette([], cassette_path)
        assert cassette_path.exists()

    def test_empty_cassette_loads_as_empty_list(self, tmp_path: Path):
        """An empty cassette file loads as an empty list."""
        cassette_path = tmp_path / "empty.jsonl"
        save_cassette([], cassette_path)
        loaded = load_cassette(cassette_path)
        assert loaded == []


# ---------- Partial corruption tolerance ----------


class TestPartialCorruptionTolerance:
    """Malformed lines are skipped, not fatal."""

    def test_malformed_json_line_skipped(self, tmp_path: Path):
        """A line with invalid JSON is skipped, valid lines still load."""
        cassette_path = tmp_path / "partial.jsonl"
        valid_line = CassetteRecord(
            type="call",
            event="cpu.status",
            response={"stepping": False},
            timestamp=1.0,
        ).to_json()
        cassette_path.write_text(
            valid_line + "\n" + "{invalid json line\n" + valid_line + "\n",
            encoding="utf-8",
        )
        loaded = load_cassette(cassette_path)
        assert len(loaded) == 2  # two valid lines, one skipped
        assert all(r.event == "cpu.status" for r in loaded)

    def test_missing_required_field_line_skipped(self, tmp_path: Path):
        """A line with valid JSON but missing required fields is skipped."""
        cassette_path = tmp_path / "missing_field.jsonl"
        valid_line = CassetteRecord(
            type="call", event="cpu.status", response={}, timestamp=1.0
        ).to_json()
        # Valid JSON, but missing "event" field.
        bad_line = json.dumps({"type": "call"})
        cassette_path.write_text(
            valid_line + "\n" + bad_line + "\n",
            encoding="utf-8",
        )
        loaded = load_cassette(cassette_path)
        assert len(loaded) == 1  # bad line skipped

    def test_empty_lines_skipped(self, tmp_path: Path):
        """Blank lines in the cassette file are skipped."""
        cassette_path = tmp_path / "blank_lines.jsonl"
        valid = CassetteRecord(
            type="call", event="cpu.status", response={}, timestamp=1.0
        ).to_json()
        cassette_path.write_text(
            f"\n{valid}\n\n{valid}\n\n",
            encoding="utf-8",
        )
        loaded = load_cassette(cassette_path)
        assert len(loaded) == 2


# ---------- Line-level independent parsing ----------


class TestLineLevelParsing:
    """Each line is independently parseable — no cross-line state."""

    def test_each_line_independently_parseable(self, tmp_path: Path):
        """Every non-empty line in a cassette must parse independently."""
        records = [
            CassetteRecord(type="call", event=f"event_{i}", response={}, timestamp=float(i))
            for i in range(10)
        ]
        cassette_path = tmp_path / "indp.jsonl"
        save_cassette(records, cassette_path)

        lines = cassette_path.read_text(encoding="utf-8").strip().split("\n")
        # Each line must independently parse to a CassetteRecord.
        for i, line in enumerate(lines):
            rec = CassetteRecord.from_json(line)
            assert rec.event == f"event_{i}"
            assert rec.timestamp == float(i)

    def test_unicode_preserved_in_jsonl(self, tmp_path: Path):
        """Non-ASCII content (e.g. Japanese text) must survive roundtrip."""
        rec = CassetteRecord(
            type="call",
            event="memory.readString",
            params={"address": 0x08808ADC},
            response={"string": "TEST-0000 セーブデータ"},
            timestamp=1.0,
        )
        cassette_path = tmp_path / "unicode.jsonl"
        save_cassette([rec], cassette_path)

        loaded = load_cassette(cassette_path)
        assert len(loaded) == 1
        assert loaded[0].response["string"] == "TEST-0000 セーブデータ"
