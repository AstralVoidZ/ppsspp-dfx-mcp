"""S1 fix tests: scan pattern cap + chunk+overlap read budget.

Real-PPSSPP evidence (review-r2 probes): PPSSPP tolerates 44–128 KiB
reads, so the "empty successful scan" failure did not reproduce at those
sizes — the fix is contract consistency (the tool layer documents a 64 KiB
single-read budget; scan's chunk+overlap read silently exceeded it) plus
defense in depth for direct client callers.
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fake_transport import FakeTransport

from ppsspp_dfx_mcp.errors import ToolError
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient
from ppsspp_dfx_mcp.tools._common import MAX_SCAN_PATTERN_BYTES
from ppsspp_dfx_mcp.tools.memory import read_memory

# ── Tool layer: pattern cap ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_rejects_oversized_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_client = AsyncMock()
    mock_client.scan_memory.return_value = []

    @asynccontextmanager
    async def fake_session_client(session_id: str):
        yield mock_client

    monkeypatch.setattr("ppsspp_dfx_mcp.tools.memory.session_client", fake_session_client)

    oversized = "41" * (MAX_SCAN_PATTERN_BYTES + 1)  # hex → cap+1 bytes
    with pytest.raises(ToolError, match="scan cap"):
        await read_memory(
            session_id="sess-1",
            action="scan",
            pattern=oversized,
            start_addr="0x08800000",
            end_addr="0x08810000",
        )
    mock_client.scan_memory.assert_not_called()


@pytest.mark.asyncio
async def test_tool_allows_pattern_at_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_client = AsyncMock()
    mock_client.scan_memory.return_value = []

    @asynccontextmanager
    async def fake_session_client(session_id: str):
        yield mock_client

    monkeypatch.setattr("ppsspp_dfx_mcp.tools.memory.session_client", fake_session_client)

    at_cap = "41" * MAX_SCAN_PATTERN_BYTES
    await read_memory(
        session_id="sess-1",
        action="scan",
        pattern=at_cap,
        start_addr="0x08800000",
        end_addr="0x08810000",
    )
    assert mock_client.scan_memory.await_count == 1


# ── Client layer: chunk+overlap stays within the read budget ─────────────


def _make_recording_read_handler(memory_map: dict[int, bytes], sizes: list[int]):
    def handler(**params: Any) -> dict[str, str]:
        addr = params["address"]
        size = params["size"]
        sizes.append(size)
        for region_addr, region_data in memory_map.items():
            if region_addr <= addr < region_addr + len(region_data):
                offset = addr - region_addr
                chunk = region_data[offset : offset + size]
                return {"base64": base64.b64encode(chunk).decode("ascii")}
        return {"base64": ""}

    return handler


@pytest.mark.asyncio
async def test_scan_single_read_never_exceeds_budget():
    """A 4096-byte pattern with chunk_size=65536 must not issue a
    chunk+overlap read above 65536 bytes (the unclamped read would be
    65536 + 4095 = 69631)."""
    region_start = 0x08800000
    region_len = 100_000
    transport = FakeTransport()
    sizes: list[int] = []
    transport.set_response(
        "memory.read",
        _make_recording_read_handler({region_start: b"\x00" * region_len}, sizes),
    )
    client = PpssppDebugClient(transport)

    pattern = b"\x11" * 4096  # not present in the zeroed region
    matches = await client.scan_memory(
        pattern=pattern,
        start=region_start,
        end=region_start + region_len,
        chunk_size=65536,
    )
    assert matches == []
    assert sizes, "scan issued no reads"
    assert max(sizes) <= 65536, f"oversized read issued: {max(sizes)}"
