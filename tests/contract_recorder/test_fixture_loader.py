"""Tests for the fixture loader (fixture_loader.py).

Verifies:
- load_all injects first-record responses into FakeTransport
- load_with_params injects callable matchers that select by params
- check_ppsspp_version detects version drift
- Missing fixture dir is handled gracefully
- Fixture file format (type / records / ppsspp_version) is parsed
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from contract_recorder.fixture_loader import (
    check_ppsspp_version,
    load_all,
    load_with_params,
)
from fake_transport import FakeTransport


def _write_fixture(
    fixture_dir: Path,
    event: str,
    records: list[dict[str, Any]],
    record_type: str = "call",
    ppsspp_version: str = "v1.0",
) -> None:
    """Helper: write a fixture file for one event."""
    fixture_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "ppsspp_version": ppsspp_version,
        "type": record_type,
        "records": records,
    }
    (fixture_dir / f"{event}.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------- load_all tests ----------


class TestLoadAll:
    """load_all injects first-record responses into FakeTransport."""

    @pytest.mark.asyncio
    async def test_load_all_injects_first_response(self, tmp_path: Path):
        fixture_dir = tmp_path / "fixtures"
        _write_fixture(fixture_dir, "memory.read_u32", [
            {"params": {"address": 0x08804000}, "response": {"value": 0xAA}},
            {"params": {"address": 0x08804004}, "response": {"value": 0xBB}},
        ])
        fake = FakeTransport()

        counts = load_all(fixture_dir, fake)

        assert counts == {"memory.read_u32": 2}
        # load_all injects the FIRST record's response.
        result = await fake.call("memory.read_u32", address=0x08804000)
        assert result == {"value": 0xAA}

    @pytest.mark.asyncio
    async def test_load_all_multiple_events(self, tmp_path: Path):
        fixture_dir = tmp_path / "fixtures"
        _write_fixture(fixture_dir, "cpu.status", [
            {"params": {}, "response": {"stepping": False}},
        ])
        _write_fixture(fixture_dir, "memory.read_u32", [
            {"params": {"address": 0x08804000}, "response": {"value": 0xDEADBEEF}},
        ])
        fake = FakeTransport()

        counts = load_all(fixture_dir, fake)

        assert set(counts.keys()) == {"cpu.status", "memory.read_u32"}
        assert await fake.call("cpu.status") == {"stepping": False}
        assert await fake.call("memory.read_u32", address=0x08804000) == {"value": 0xDEADBEEF}

    @pytest.mark.asyncio
    async def test_load_all_skips_fire_and_forget_fixtures(self, tmp_path: Path):
        """fire_and_forget fixtures have no response — not injected via set_response."""
        fixture_dir = tmp_path / "fixtures"
        _write_fixture(
            fixture_dir, "cpu.stepping",
            [{"params": {"step": "into"}}],
            record_type="fire_and_forget",
        )
        fake = FakeTransport()

        counts = load_all(fixture_dir, fake)

        # Counted but not injected (no set_response call for faf).
        assert counts == {"cpu.stepping": 1}
        # No response registered for cpu.stepping — call returns empty dict.
        result = await fake.call("cpu.stepping", step="into")
        assert result == {}

    def test_load_all_missing_dir_returns_empty(self, tmp_path: Path):
        """Missing fixture dir logs a warning and returns empty dict."""
        fake = FakeTransport()
        counts = load_all(tmp_path / "nonexistent", fake)
        assert counts == {}


# ---------- load_with_params tests ----------


class TestLoadWithParams:
    """load_with_params injects callable matchers that select by params."""

    @pytest.mark.asyncio
    async def test_load_with_params_exact_match(self, tmp_path: Path):
        fixture_dir = tmp_path / "fixtures"
        _write_fixture(fixture_dir, "memory.read_u32", [
            {"params": {"address": 0x08804000}, "response": {"value": 1}},
            {"params": {"address": 0x08804004}, "response": {"value": 2}},
        ])
        fake = FakeTransport()

        load_with_params(fixture_dir, fake)

        # Exact param match returns the right response.
        r1 = await fake.call("memory.read_u32", address=0x08804000)
        assert r1 == {"value": 1}
        r2 = await fake.call("memory.read_u32", address=0x08804004)
        assert r2 == {"value": 2}

    @pytest.mark.asyncio
    async def test_load_with_params_fallback_to_first(self, tmp_path: Path):
        """When no exact param match, falls back to first record."""
        fixture_dir = tmp_path / "fixtures"
        _write_fixture(fixture_dir, "memory.read_u32", [
            {"params": {"address": 0x08804000}, "response": {"value": 1}},
            {"params": {"address": 0x08804004}, "response": {"value": 2}},
        ])
        fake = FakeTransport()

        load_with_params(fixture_dir, fake)

        # Unmatched params → first record's response (fallback).
        r = await fake.call("memory.read_u32", address=0x99999999)
        assert r == {"value": 1}


# ---------- check_ppsspp_version tests ----------


class TestCheckPpssppVersion:
    """Version drift detection."""

    def test_returns_versions_found(self, tmp_path: Path, caplog):
        fixture_dir = tmp_path / "fixtures"
        _write_fixture(fixture_dir, "a", [{"params": {}, "response": {}}], ppsspp_version="v1.0")
        _write_fixture(fixture_dir, "b", [{"params": {}, "response": {}}], ppsspp_version="v2.0")

        versions = check_ppsspp_version(fixture_dir)

        assert "v1.0" in versions
        assert "v2.0" in versions

    def test_logs_warning_on_drift(self, tmp_path: Path, caplog):
        import logging

        fixture_dir = tmp_path / "fixtures"
        _write_fixture(fixture_dir, "a", [{"params": {}, "response": {}}], ppsspp_version="v1.0")

        with caplog.at_level(logging.WARNING):
            check_ppsspp_version(fixture_dir, expected_version="v2.0")

        assert any("ppsspp_version mismatch" in r.message for r in caplog.records)

    def test_no_warning_when_version_matches(self, tmp_path: Path, caplog):
        import logging

        fixture_dir = tmp_path / "fixtures"
        _write_fixture(fixture_dir, "a", [{"params": {}, "response": {}}], ppsspp_version="v1.0")

        with caplog.at_level(logging.WARNING):
            check_ppsspp_version(fixture_dir, expected_version="v1.0")

        assert not any("mismatch" in r.message for r in caplog.records)

    def test_unknown_version_does_not_trigger_warning(self, tmp_path: Path, caplog):
        import logging

        fixture_dir = tmp_path / "fixtures"
        _write_fixture(fixture_dir, "a", [{"params": {}, "response": {}}], ppsspp_version="unknown")

        with caplog.at_level(logging.WARNING):
            check_ppsspp_version(fixture_dir, expected_version="v2.0")

        # "unknown" version never triggers drift warning.
        assert not any("mismatch" in r.message for r in caplog.records)


# ---------- Integration: load real fixtures if available ----------


class TestRealFixturesAvailable:
    """If the real PPSSPP fixtures exist (from record_fixtures.py),
    verify they load without error."""

    REAL_FIXTURES = Path(__file__).resolve().parents[1] / "cassettes" / "fixtures"

    @pytest.mark.skipif(
        not REAL_FIXTURES.is_dir(),
        reason="real fixtures not recorded yet (run record_fixtures.py)",
    )
    @pytest.mark.asyncio
    async def test_real_fixtures_load_all(self):
        """Load the real recorded fixtures and verify they're non-empty."""
        fake = FakeTransport()
        counts = load_all(self.REAL_FIXTURES, fake)
        assert len(counts) > 0, "no fixtures loaded"
        # cpu.status should be present (always-recorded event).
        assert "cpu.status" in counts
        # cpu.status response should have stepping field.
        result = await fake.call("cpu.status")
        assert "stepping" in result

    @pytest.mark.skipif(
        not REAL_FIXTURES.is_dir(),
        reason="real fixtures not recorded yet (run record_fixtures.py)",
    )
    @pytest.mark.asyncio
    async def test_real_fixtures_memory_read_u32(self):
        """memory.read_u32 fixture should return a value field."""
        fake = FakeTransport()
        load_all(self.REAL_FIXTURES, fake)
        result = await fake.call("memory.read_u32", address=0x08804000)
        assert "value" in result
