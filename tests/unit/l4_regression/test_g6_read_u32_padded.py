"""G-6 (FR-006): read_u32 text must render fixed-width zero-padded hex.

Deep-test case (mcp_test_report/tools/ppsspp_read_memory.md): a u32 read
of 0 rendered its hex form as '(0x0)' — an ad-hoc substring comparison
against '0x0)' misfires once values like 0x0A come along, and the width
no longer matches the 8-digit address convention used everywhere else.

Fix: f"0x{value:08X}" in the read_u32 text branch.

Two-way acceptance (spec.md FR-006): value 0 MUST contain '0x00000000'
and MUST NOT contain '0x0)'; control value 0x1 MUST render
'0x00000001' (so the assertion cannot pass by rendering everything as
a single format string).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from ppsspp_dfx_mcp.tools import memory as memory_mod
from ppsspp_dfx_mcp.tools.memory import read_memory
from ppsspp_dfx_mcp.views.memory import _format_read_text

ADDR = 0x08804000


class _FakeClient:
    def __init__(self, value: int) -> None:
        self.value = value

    async def read_u32(self, address: int) -> int:
        return self.value


def _patch_client(monkeypatch, value: int) -> None:
    client = _FakeClient(value)

    @asynccontextmanager
    async def _fake_session_client(_session_id):
        yield client

    monkeypatch.setattr(memory_mod, "session_client", _fake_session_client)


class TestViewFormatting:
    def test_zero_is_padded(self) -> None:
        text = _format_read_text("read_u32", ADDR, 0, 4)
        assert text == "0x08804000: 0 (0x00000000)"
        assert "0x0)" not in text

    def test_one_is_padded(self) -> None:
        assert _format_read_text("read_u32", ADDR, 1, 4) == "0x08804000: 1 (0x00000001)"

    def test_full_width_value_unchanged(self) -> None:
        assert _format_read_text("read_u32", ADDR, 0xDEADBEEF, 4) == (
            "0x08804000: 3735928559 (0xDEADBEEF)"
        )

    def test_max_u32_uses_full_width(self) -> None:
        assert _format_read_text("read_u32", ADDR, 0xFFFFFFFF, 4) == (
            "0x08804000: 4294967295 (0xFFFFFFFF)"
        )

    def test_non_int_value_passes_through(self) -> None:
        assert _format_read_text("read_u32", ADDR, "N/A", 4) == "0x08804000: N/A"


class TestToolText:
    """End-to-end through the tool, as the deep test observed it."""

    async def test_tool_zero_read_is_padded(self, monkeypatch) -> None:
        _patch_client(monkeypatch, 0)
        out = await read_memory(session_id="sess-1", action="read_u32", address="0x08804000")
        assert out["text"] == "0x08804000: 0 (0x00000000)"
        assert "0x0)" not in out["text"]

    async def test_tool_nonzero_read_is_padded(self, monkeypatch) -> None:
        _patch_client(monkeypatch, 0x0A)
        out = await read_memory(session_id="sess-1", action="read_u32", address="0x08804000")
        assert out["text"] == "0x08804000: 10 (0x0000000A)"
