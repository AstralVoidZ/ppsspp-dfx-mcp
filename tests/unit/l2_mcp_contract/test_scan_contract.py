"""ppsspp_scan contract tests — three-mode parameter validation + value
narrow lifecycle (merged span reads) against a scripted fake client.

Fake-mode: session_client is monkeypatched to yield a fake client whose
read_bytes returns deterministic bytes over a 64 KiB window, so the value
scan phases run end-to-end without PPSSPP.
"""

from __future__ import annotations

import pytest

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
