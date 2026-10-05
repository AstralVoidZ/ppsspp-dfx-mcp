"""G-16 (FR-016): read_string must separate "empty" from "address invalid".

Deep-test cases P2-09 / P2-10 (mcp_test_report/tools/ppsspp_read_memory.md):
read_string at 0x089961C0 -- a known string literal in the Julian module --
answered value="" / size=0 during the title screen, because that literal is
not resident yet. The tool said nothing, so a caller could only read "" as
"the string is empty", when the truth was "this address holds nothing in the
current state".

PPSSPP answers an unreadable address with ZEROS, not an error, so at the byte
level a not-yet-loaded address (00 00 00 ...) and a genuinely empty string
(NUL in the first byte) are indistinguishable. The fix therefore does not
claim to KNOW which happened -- it states that the two are being confused
and that the caller must verify:

  size > 0   -> no caveat; a real string came back
  size == 0   -> text carries the "may be unreadable / not yet loaded" note

Two-way acceptance (spec.md FR-016): the empty read MUST carry the note, and
a non-empty read MUST NOT be polluted by it (a caveat on every read would be
noise, and would train callers to ignore it).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.tools.memory import read_memory

# The literal address from the deep-test report.
LITERAL_ADDR = "0x089961C0"


def _patch_client(monkeypatch: pytest.MonkeyPatch, value: str) -> AsyncMock:
    """read_string returns `value` for any address."""
    mock = AsyncMock()
    mock.read_string.return_value = value

    @asynccontextmanager
    async def fake_session_client(session_id: str) -> AsyncIterator[AsyncMock]:
        yield mock

    monkeypatch.setattr("ppsspp_dfx_mcp.tools.memory.session_client", fake_session_client)
    return mock


class TestEmptyReadIsFlagged:
    """size==0 MUST carry an explicit 'this may not be a valid string' note."""

    async def test_empty_read_carries_caveat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, "")
        out = await read_memory(action="read_string", address=LITERAL_ADDR, session_id="sess-1")
        assert out["value"] == ""
        assert out["size"] == 0
        assert "may be" in out["text"], out["text"]
        assert "unreadable" in out["text"], out["text"]
        # The note must not pretend the string IS empty -- that is the whole
        # point (the two states are indistinguishable from the value alone).
        assert "not yet loaded" in out["text"], out["text"]

    async def test_caveat_names_the_address(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fails if the note drops the address, leaving the caller unable to
        tell WHICH address is in question.

        Scoped to the note substring AFTER the 'empty string at' marker: the
        base text line always carries the address, so a whole-text check
        would pass even with a causeless note.
        """
        _patch_client(monkeypatch, "")
        out = await read_memory(action="read_string", address=LITERAL_ADDR, session_id="sess-1")
        note = out["text"].split("empty string at", 1)[1]
        assert note.lstrip().startswith(LITERAL_ADDR), out["text"]

    async def test_caveat_is_not_silently_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fails if the note is computed but dropped before the response."""
        _patch_client(monkeypatch, "")
        out = await read_memory(action="read_string", address=LITERAL_ADDR, session_id="sess-1")
        assert "empty string at" in out["text"], out["text"]


class TestNonEmptyReadIsNotFlagged:
    """Two-way guard: a real string MUST NOT get the caveat.

    A caveat on every read would be noise that trains callers to skip it.
    """

    async def test_real_string_has_no_caveat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, "sample.dat")
        out = await read_memory(action="read_string", address=LITERAL_ADDR, session_id="sess-1")
        assert out["value"] == "sample.dat"
        assert out["size"] == len("sample.dat")
        assert "unreadable" not in out["text"], out["text"]
        assert out["text"] == "0x089961C0: 'sample.dat'", out["text"]

    async def test_string_with_nul_inside_is_not_flagged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a ZERO-LENGTH result is ambiguous; 'a\\x00b' never reaches
        here (the client cuts at the first NUL), but a non-empty value with
        size>0 must stay unflagged."""
        _patch_client(monkeypatch, "ab")
        out = await read_memory(action="read_string", address=LITERAL_ADDR, session_id="sess-1")
        assert out["size"] == 2
        assert "unreadable" not in out["text"], out["text"]

    async def test_other_actions_never_get_the_caveat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """read_u32 returning 0 is a REAL zero, not an empty string."""
        mock = _patch_client(monkeypatch, "")
        mock.read_u32.return_value = 0
        out = await read_memory(action="read_u32", address=LITERAL_ADDR, session_id="sess-1")
        assert out["value"] == 0
        assert "unreadable" not in out["text"], out["text"]


class TestCaveatIsAdditiveOnly:
    """The caveat appends; it must not clobber the read's own text."""

    async def test_text_still_starts_with_the_address_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_client(monkeypatch, "")
        out = await read_memory(action="read_string", address=LITERAL_ADDR, session_id="sess-1")
        assert out["text"].startswith("0x089961C0: ''"), out["text"]
