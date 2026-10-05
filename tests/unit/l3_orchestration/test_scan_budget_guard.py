"""Scan budget guard tests (v0.1.7) — auto-background, read timeouts,
background wall-clock budget.

Real-PPSSPP evidence (2026-09-30, 24 MB full-band pattern scan):
foreground @ 4 KiB chunks = 53-96 s (build-dependent) — always past the
~30s MCP client timeout; background @ 64 KiB = 3.4-40 s. The guard makes
the over-budget case mechanically impossible instead of advice.
"""

from __future__ import annotations

import asyncio

import pytest

from ppsspp_dfx_mcp.core.batch_jobs import get_registry
from ppsspp_dfx_mcp.service import scan_engine as scan_engine_mod
from ppsspp_dfx_mcp.tools import scan as scan_mod
from ppsspp_dfx_mcp.tools.scan import scan

TWO_MIB = 2 * 1024 * 1024
BASE = bytes((i * 11 + 5) % 256 for i in range(0x4000))


class FakeClient:
    """Deterministic reads over a repeating window (addresses wrap)."""

    async def read_bytes(self, address: int, size: int) -> list[int]:
        offset = address % len(BASE)
        return [BASE[(offset + i) % len(BASE)] for i in range(size)]

    async def scan_memory(
        self,
        pattern: bytes,
        start: int,
        end: int,
        max_results: int = 100,
        chunk_size: int = 65536,
    ) -> list[dict[str, object]]:
        """Client-side chunked scan mirroring DebugClient.scan_memory."""
        matches: list[dict[str, object]] = []
        cursor = start
        while cursor < end and len(matches) < max_results:
            size = min(chunk_size, end - cursor)
            data = bytes(await self.read_bytes(cursor, size))
            idx = data.find(pattern)
            while idx != -1 and len(matches) < max_results:
                matches.append({"address": cursor + idx, "context": pattern.hex().upper()})
                idx = data.find(pattern, idx + 1)
            cursor += size
        return matches


class FakeSessionClient:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    async def __aenter__(self) -> FakeClient:
        return self._client

    async def __aexit__(self, *exc) -> None:
        return None


async def _resolve(session_id: str | None) -> str:
    return session_id or "sess-fake"


@pytest.fixture()
def fake_scan(monkeypatch: pytest.MonkeyPatch):
    client = FakeClient()
    monkeypatch.setattr(scan_mod, "session_client", lambda session_id: FakeSessionClient(client))
    # Pattern mode runs through the extracted service (W19), which opens
    # session_client from its own module globals.
    monkeypatch.setattr(
        scan_engine_mod, "session_client", lambda session_id: FakeSessionClient(client)
    )
    monkeypatch.setattr(scan_mod, "resolve_session_id", _resolve)

    async def _alive(session_id: str) -> None:
        return None

    monkeypatch.setattr(scan_mod, "validate_session_alive", _alive)
    scan_mod.reset_value_sessions()
    return client


async def _await_terminal(batch_id: str, *, timeout_s: float = 10.0) -> str:
    job = get_registry().get(batch_id)
    assert job is not None
    deadline = asyncio.get_event_loop().time() + timeout_s
    while job.status not in ("completed", "failed", "cancelled"):
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"job {batch_id} did not finish: {job.status}")
        await asyncio.sleep(0.02)
    return job.status


# ── Auto-background routing ──────────────────────────────────────────────


async def test_pattern_over_limit_auto_backgrounds(fake_scan):
    span = TWO_MIB + 1
    r = await scan(
        mode="pattern",
        pattern="AABB",
        pattern_type="hex",
        start_addr="0x08804000",
        end_addr=f"0x{0x08804000 + span:08X}",
        session_id="sess-fake",
    )
    assert r["action"] == "submitted"
    assert await _await_terminal(r["batch_id"]) == "completed"


async def test_strings_over_limit_auto_backgrounds(fake_scan):
    span = TWO_MIB + 1024
    r = await scan(
        mode="strings",
        charset="ascii",
        start_addr="0x08804000",
        end_addr=f"0x{0x08804000 + span:08X}",
        session_id="sess-fake",
    )
    assert r["action"] == "submitted"
    assert await _await_terminal(r["batch_id"]) == "completed"


async def test_pattern_under_limit_stays_foreground(fake_scan):
    r = await scan(
        mode="pattern",
        pattern="AABB",
        pattern_type="hex",
        start_addr="0x08804000",
        end_addr="0x08904000",  # 1 MiB — inside the foreground budget
        session_id="sess-fake",
    )
    assert "action" not in r or r.get("action") != "submitted"


async def test_value_foreground_cap_contract_unchanged(fake_scan):
    """value-initial keeps its own 8 MiB hard-cap error (no auto-bg)."""
    span = TWO_MIB + 1  # over the pattern soft limit, under the 8 MiB cap
    r = await scan(
        mode="value",
        phase="initial",
        value=1,
        width="u32",
        start_addr="0x08804000",
        end_addr=f"0x{0x08804000 + span:08X}",
        session_id="sess-fake",
    )
    assert r.get("action") != "submitted"
    assert r["scan_handle"]
    # T053 S-4：尾清理走生产语义化回收 API，不再直写 _VALUE_SESSIONS。
    scan_mod.reset_value_sessions()


# ── Background wall-clock budget ─────────────────────────────────────────


class SlowClient(FakeClient):
    async def read_bytes(self, address: int, size: int) -> list[int]:
        await asyncio.sleep(0.2)
        return await super().read_bytes(address, size)


async def test_bg_budget_exceeded_fails_job_cleanly(fake_scan, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(scan_mod, "SCAN_BG_BUDGET_S", 0.05)
    monkeypatch.setattr(
        scan_mod, "session_client", lambda session_id: FakeSessionClient(SlowClient())
    )
    monkeypatch.setattr(
        scan_engine_mod, "session_client", lambda session_id: FakeSessionClient(SlowClient())
    )
    r = await scan(
        mode="pattern",
        pattern="AABB",
        pattern_type="hex",
        start_addr="0x08804000",
        end_addr="0x08904000",
        background=True,
        session_id="sess-fake",
    )
    status = await _await_terminal(r["batch_id"])
    job = get_registry().get(r["batch_id"])
    assert status == "failed"
    assert "budget" in job.error.lower()


# ── Per-chunk read timeout (service layer) ───────────────────────────────


async def test_scan_memory_consecutive_timeouts_abort(monkeypatch):
    from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient
    from tests.fake_transport import FakeTransport

    client = PpssppDebugClient(FakeTransport())
    calls: list[int] = []

    async def hang(address: int, size: int) -> list[int]:
        calls.append(address)
        raise TimeoutError()

    monkeypatch.setattr(client, "read_bytes", hang)
    with pytest.raises(RuntimeError, match="timed out"):
        await client.scan_memory(
            pattern=b"\x11\x22", start=0x08800000, end=0x09000000, chunk_size=65536
        )
    # 5 tolerated + 1 that trips the abort.
    assert len(calls) == 6


async def test_scan_memory_plain_errors_still_skip(monkeypatch):
    """Unmapped regions (plain exceptions) keep the silent-skip contract —
    only timeouts count toward the abort."""
    from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient
    from tests.fake_transport import FakeTransport

    client = PpssppDebugClient(FakeTransport())
    calls: list[int] = []

    async def unmapped(address: int, size: int) -> list[int]:
        calls.append(address)
        raise ConnectionError("unmapped")

    monkeypatch.setattr(client, "read_bytes", unmapped)
    matches = await client.scan_memory(
        pattern=b"\x11\x22", start=0x08800000, end=0x09000000, chunk_size=65536
    )
    assert matches == []
    # Every chunk skipped, no abort (8 MiB @ 64 KiB = 128 chunks + tail).
    assert len(calls) > 6


async def test_read_segments_timeouts_abort_tool_level(fake_scan, monkeypatch: pytest.MonkeyPatch):
    """_iter_segments (value/strings path) aborts after consecutive
    timeouts and the tool surfaces a clean error instead of hanging."""

    class HangingClient(FakeClient):
        async def read_bytes(self, address: int, size: int) -> list[int]:
            raise TimeoutError()

    monkeypatch.setattr(
        scan_mod, "session_client", lambda session_id: FakeSessionClient(HangingClient())
    )
    from ppsspp_dfx_mcp.errors import ToolError

    with pytest.raises(ToolError, match="timed out"):
        await scan(
            mode="value",
            phase="initial",
            value=1,
            width="u32",
            start_addr="0x08804000",
            end_addr="0x08A04000",  # 2 MiB — under the 8 MiB value cap
            session_id="sess-fake",
        )
