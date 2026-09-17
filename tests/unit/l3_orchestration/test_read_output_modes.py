"""L3 orchestration tests: G1 read_bytes output-mode governance.

Anchor: research_ppsspp_dfx_best_practice_gap_audit_v1 §G1 — a 65536-byte
read_bytes response previously carried the payload THREE ways (int array
in `value` ≈ 4 chars/byte, full hex dump in `text` ≈ 3 chars/byte, plus
the SDK text channel), with no disk side-channel. The `output` parameter
adds two lighter channels without changing the default.

L3 focus (tool wrapper orchestration, NOT WS forwarding):
- output="value" (default): byte list + hex text — unchanged contract
- output="hex": value=None, hex dump kept in `text` (~half the characters)
- output="file": raw .bin + .hex.txt under output/memory_reads/, response
  carries paths + a 64-byte preview only
- output is ignored for non-read_bytes actions
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.tools.memory import read_memory

pytestmark = pytest.mark.asyncio

_SAMPLE = bytes(range(64)) * 2  # 128 bytes, deterministic


def _patch_output_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect .ppsspp-dfx/output to a pytest tmp dir."""

    def fake_output_dir() -> Path:
        d = tmp_path / ".ppsspp-dfx" / "output"
        d.mkdir(parents=True, exist_ok=True)
        return d

    monkeypatch.setattr("ppsspp_dfx_mcp.tools._common.output_dir", fake_output_dir)


def _mock_read_bytes(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> AsyncMock:
    mock_client = AsyncMock()
    mock_client.read_bytes.return_value = payload

    @asynccontextmanager
    async def fake_session_client(
        session_id: str,
    ) -> AsyncIterator[AsyncMock]:
        yield mock_client

    monkeypatch.setattr("ppsspp_dfx_mcp.tools.memory.session_client", fake_session_client)
    return mock_client


class TestReadBytesOutputModes:
    async def test_output_value_default_keeps_byte_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default output='value': byte list + hex text (unchanged contract)."""
        _mock_read_bytes(monkeypatch, _SAMPLE)
        result = await read_memory(
            action="read_bytes",
            address="0x08804000",
            size=len(_SAMPLE),
            session_id="sess-1",
        )
        assert result["value"] == list(_SAMPLE)
        assert "00 01 02" in result["text"]
        assert result["file"] == ""

    async def test_output_hex_drops_byte_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """output='hex': value=None, hex dump kept — response ~half the size."""
        _mock_read_bytes(monkeypatch, _SAMPLE)
        value_result = await read_memory(
            action="read_bytes",
            address="0x08804000",
            size=len(_SAMPLE),
            output="value",
            session_id="sess-1",
        )
        hex_result = await read_memory(
            action="read_bytes",
            address="0x08804000",
            size=len(_SAMPLE),
            output="hex",
            session_id="sess-1",
        )
        assert hex_result["value"] is None
        assert "00 01 02" in hex_result["text"]
        value_len = len(json.dumps(value_result))
        hex_len = len(json.dumps(hex_result))
        assert hex_len < value_len * 0.6, (
            "G1: output='hex' must drop the int-array channel — response "
            f"should shrink well below the value mode ({hex_len} vs {value_len})"
        )

    async def test_output_file_writes_dual_files(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """output='file': .bin + .hex.txt on disk, paths + preview in response."""
        _patch_output_dir(monkeypatch, tmp_path)
        _mock_read_bytes(monkeypatch, _SAMPLE)
        result = await read_memory(
            action="read_bytes",
            address="0x08804000",
            size=len(_SAMPLE),
            output="file",
            session_id="sess-1",
        )
        assert result["value"] is None
        bin_path = Path(result["file"])
        assert bin_path.name == "mem_08804000_128.bin"
        assert bin_path.read_bytes() == _SAMPLE
        hex_path = bin_path.with_name(bin_path.name + ".hex.txt")
        assert hex_path.exists()
        assert hex_path.read_text(encoding="utf-8").startswith("00 01 02")
        # Response carries paths + preview only — no full payload channel.
        assert f"saved {len(_SAMPLE)} bytes to" in result["text"]
        assert "preview:" in result["text"]
        assert "3F" in result["text"]  # preview covers up to byte 0x3F
        assert "..." in result["text"]  # 128 > 64 → elided marker
        assert len(json.dumps(result)) < 1000, (
            "G1: output='file' response must stay O(preview), not O(payload)"
        )

    async def test_output_ignored_for_read_u32(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """output is a read_bytes knob — read_u32 ignores it entirely."""
        mock_client = AsyncMock()
        mock_client.read_u32.return_value = 0x1234

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr("ppsspp_dfx_mcp.tools.memory.session_client", fake_session_client)
        result = await read_memory(
            action="read_u32",
            address="0x08804000",
            output="file",
            session_id="sess-1",
        )
        assert result["value"] == 0x1234
        assert result["file"] == ""
        assert result["text"] == "0x08804000: 4660 (0x1234)"


class TestReadBytesFilePreviewBoundaries:
    async def test_preview_exact_at_64_bytes(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A 64-byte read previews fully without the elision marker."""
        _patch_output_dir(monkeypatch, tmp_path)
        _mock_read_bytes(monkeypatch, bytes(range(64)))
        result = await read_memory(
            action="read_bytes",
            address="0x08804000",
            size=64,
            output="file",
            session_id="sess-1",
        )
        assert "..." not in result["text"]
        assert "3F" in result["text"]  # last previewed byte 0x3F
