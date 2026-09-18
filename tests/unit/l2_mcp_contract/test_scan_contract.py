"""ppsspp_scan contract tests — three-mode parameter validation + value
narrow lifecycle (merged span reads) against a scripted fake client.

Fake-mode: session_client is monkeypatched to yield a fake client whose
read_bytes returns deterministic bytes over a 64 KiB window, so the value
scan phases run end-to-end without PPSSPP.
"""

from __future__ import annotations

import asyncio

import pytest

from ppsspp_dfx_mcp.core.batch_jobs import get_registry
from ppsspp_dfx_mcp.tools import scan as scan_mod
from ppsspp_dfx_mcp.tools.scan import scan

BASE = bytes((i * 11 + 5) % 256 for i in range(0x4000))


class FakeClient:
    """Deterministic reads over a repeating window (addresses wrap)."""

    async def read_bytes(self, address: int, size: int) -> list[int]:
        offset = address % len(BASE)
        out = []
        for i in range(size):
            out.append(BASE[(offset + i) % len(BASE)])
        return out


class FakeSessionClient:
    def __init__(self, client: FakeClient) -> None:
        self._client = client

    async def __aenter__(self) -> FakeClient:
        return self._client

    async def __aexit__(self, *exc) -> None:
        return None


@pytest.fixture()
def fake_scan(monkeypatch: pytest.MonkeyPatch):
    client = FakeClient()
    monkeypatch.setattr(scan_mod, "session_client", lambda session_id: FakeSessionClient(client))
    monkeypatch.setattr(scan_mod, "resolve_session_id", _resolve)

    async def _alive(session_id: str) -> None:
        return None

    monkeypatch.setattr(scan_mod, "validate_session_alive", _alive)
    scan_mod._reset_value_sessions_for_tests()
    return client


async def _resolve(session_id: str | None) -> str:
    return session_id or "sess-fake"


async def test_pattern_requires_pattern_arg(fake_scan):
    with pytest.raises(scan_mod.ArgsInvalid, match="pattern is required"):
        await scan(mode="pattern", start_addr="0x08804000", end_addr="0x08805000")


async def test_value_initial_returns_handle_and_caps_range(fake_scan):
    r = await scan(
        mode="value",
        phase="initial",
        value=0x41,
        width="u8",
        start_addr="0x08804000",
        end_addr="0x08805000",
        session_id="sess-fake",
    )
    assert r["scan_handle"]
    assert r["candidates"] >= 0
    assert len(scan_mod._VALUE_SESSIONS) == 1


async def test_value_initial_rejects_over_hard_cap(fake_scan):
    with pytest.raises(scan_mod.ArgsInvalid, match="hard cap"):
        await scan(
            mode="value",
            phase="initial",
            value=1,
            width="u32",
            start_addr="0x08804000",
            end_addr="0x0C004000",
            session_id="sess-fake",
        )


async def test_narrow_unknown_handle_raises(fake_scan):
    with pytest.raises(scan_mod.ArgsInvalid, match="unknown scan_handle"):
        await scan(
            mode="value", phase="narrow", value=1, scan_handle="nope", session_id="sess-fake"
        )


async def test_narrow_rejects_cross_session_handle(fake_scan):
    r = await scan(
        mode="value",
        phase="initial",
        value=0x41,
        width="u8",
        start_addr="0x08804000",
        end_addr="0x08805000",
        session_id="sess-A",
    )
    handle = r["scan_handle"]
    with pytest.raises(scan_mod.ArgsInvalid, match="belongs to session"):
        await scan(
            mode="value", phase="narrow", value=0x41, scan_handle=handle, session_id="sess-B"
        )
    scan_mod._VALUE_SESSIONS.pop(handle, None)


async def test_value_lifecycle_narrow_list_drop(fake_scan):
    init = await scan(
        mode="value",
        phase="initial",
        value=0x41,
        width="u8",
        start_addr="0x08804000",
        end_addr="0x08805000",
        session_id="sess-fake",
    )
    handle = init["scan_handle"]
    narrow = await scan(
        mode="value", phase="narrow", value=0x41, scan_handle=handle, session_id="sess-fake"
    )
    assert narrow["candidates"] >= 0
    listing = await scan(mode="value", phase="list", scan_handle=handle, session_id="sess-fake")
    assert listing["scan_handle"] == handle
    drop = await scan(mode="value", phase="drop", scan_handle=handle, session_id="sess-fake")
    assert drop["dropped"] is True
    assert handle not in scan_mod._VALUE_SESSIONS


async def test_strings_returns_entries_with_addresses(fake_scan):
    r = await scan(
        mode="strings",
        charset="ascii",
        min_len=6,
        start_addr="0x08804000",
        end_addr="0x08805000",
        session_id="sess-fake",
    )
    assert r["charset"] == "ascii"
    for entry in r["strings"]:
        assert set(entry) == {"address", "text"}


async def test_background_submission_runs_to_completion(fake_scan):
    r = await scan(
        mode="value",
        phase="initial",
        value=0x41,
        width="u8",
        start_addr="0x08804000",
        end_addr="0x08805000",
        background=True,
        session_id="sess-fake",
    )
    assert r["action"] == "submitted"
    batch_id = r["batch_id"]
    job = get_registry().get(batch_id)
    assert job is not None
    for _ in range(100):
        if job.status in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)
    assert job.status == "completed", job.error
    assert job.result["scan_handle"]
    assert job.result["candidates"] >= 0


async def test_background_value_cap_lifted_to_32mib(fake_scan):
    # 前台 8MiB 硬上限会拒绝；后台放宽到 32MiB → 提交成功
    span = 9 * 1024 * 1024
    r = await scan(
        mode="value",
        phase="initial",
        value=1,
        width="u32",
        start_addr="0x08804000",
        end_addr=f"0x{0x08804000 + span:08X}",
        background=True,
        session_id="sess-fake",
    )
    assert r["action"] == "submitted"
    batch_id = r["batch_id"]
    job = get_registry().get(batch_id)
    for _ in range(100):
        if job.status in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)
    assert job.status == "completed", job.error


async def test_second_background_scan_on_same_session_rejected(fake_scan):
    r1 = await scan(
        mode="value",
        phase="initial",
        value=0x41,
        width="u8",
        start_addr="0x08804000",
        end_addr="0x08805000",
        background=True,
        session_id="sess-fake",
    )
    first_id = r1["batch_id"]
    first = get_registry().get(first_id)
    with pytest.raises(scan_mod.ArgsInvalid, match="already has a background"):
        await scan(
            mode="value",
            phase="initial",
            value=0x41,
            width="u8",
            start_addr="0x08804000",
            end_addr="0x08805000",
            background=True,
            session_id="sess-fake",
        )
    for _ in range(100):
        if first.status in ("completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.05)
    scan_mod._VALUE_SESSIONS.clear()
