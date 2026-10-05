"""G-3 (FR-003): query action='register' must carry the register name.

Deep-test cases P2-18/P2-19/P5-33 (mcp_test_report/tools/ppsspp_query.md):
ppsspp_query(action='register', name='t0') answered data={register: 8,
uintValue: 3735928559, ...} with text="". The caller had to know on its
own that register=8 is 't0' and convert uintValue to hex — the tool's
answer could not be verified against the request.

Fix: the tool echoes the normalized requested name into `data.name`;
the view renders 'name = 0xVALUE' into `text`.

Two-way acceptance (spec.md FR-003): text MUST contain the register name
and a 0x-prefixed value; data MUST contain `name`. Boundary: a
non-canonical request ('r8') echoes the name PPSSPP actually looked up
('t0'), and the plural 'registers' rendering must stay intact.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from ppsspp_dfx_mcp.tools import query as query_mod
from ppsspp_dfx_mcp.tools.query import query
from ppsspp_dfx_mcp.views.query import _format_query_text

# P5-33 evidence payload: t0 == 0xDEADBEEF.
GET_REG_PAYLOAD = {
    "event": "cpu.getReg",
    "category": 0,
    "register": 8,
    "uintValue": 0xDEADBEEF,
    "floatValue": "0.0",
}


class _FakeClient:
    def __init__(self) -> None:
        self.get_reg_calls: list[dict[str, Any]] = []

    async def get_reg(self, name: str, thread: int | None = None) -> dict[str, Any]:
        self.get_reg_calls.append({"name": name, "thread": thread})
        return dict(GET_REG_PAYLOAD)

    def with_stepping(self):
        return _noop_cm()


@asynccontextmanager
async def _noop_cm():
    yield None


def _patch_client(monkeypatch) -> _FakeClient:
    client = _FakeClient()

    @asynccontextmanager
    async def _fake_session_client(_session_id):
        yield client

    monkeypatch.setattr(query_mod, "session_client", _fake_session_client)
    return client


class TestToolEchoesRegisterName:
    """The tool layer: data.name echo survives the view normalization."""

    async def test_canonical_name_is_echoed_and_rendered(self, monkeypatch) -> None:
        _patch_client(monkeypatch)
        out = await query(session_id="sess-1", action="register", name="t0", safe=False)

        assert out["data"]["name"] == "t0"
        assert out["data"]["uintValue"] == 0xDEADBEEF
        assert out["text"] == "t0 = 0xDEADBEEF", (
            f"register text must name the register and its hex value: {out['text']!r}"
        )

    async def test_numeric_request_echoes_the_abi_name(self, monkeypatch) -> None:
        """'r8' is looked up by PPSSPP as 't0' — the echo shows that."""
        client = _patch_client(monkeypatch)
        out = await query(session_id="sess-1", action="register", name="r8", safe=False)

        assert client.get_reg_calls == [{"name": "r8", "thread": None}]
        assert out["data"]["name"] == "t0"
        assert out["text"] == "t0 = 0xDEADBEEF"

    async def test_safe_true_path_also_echoes(self, monkeypatch) -> None:
        """safe=true (paused, high-trust read) is the default path."""
        _patch_client(monkeypatch)
        out = await query(session_id="sess-1", action="register", name="pc", safe=True)

        assert out["data"]["name"] == "pc"
        assert out["trust_level"] == "high"
        assert out["text"] == "pc = 0xDEADBEEF"


class TestViewFormattingBoundaries:
    """View layer: exact line format, zero padding, and degradation."""

    def test_single_line_is_name_equals_hex(self) -> None:
        text = _format_query_text("register", {"name": "pc", "uintValue": 0x088EF0F4})
        assert text == "pc = 0x088EF0F4"

    def test_zero_value_is_zero_padded(self) -> None:
        text = _format_query_text("register", {"name": "zero", "uintValue": 0})
        assert text == "zero = 0x00000000"

    def test_missing_name_degrades_to_empty(self) -> None:
        """No echo (unexpected shape) must not fabricate a name."""
        assert _format_query_text("register", {"register": 8, "uintValue": 1}) == ""

    def test_non_dict_data_degrades_to_empty(self) -> None:
        assert _format_query_text("register", None) == ""
        assert _format_query_text("register", [1, 2]) == ""

    def test_other_actions_still_have_no_text(self) -> None:
        assert _format_query_text("game_state", {"game": {"id": "ULJS-00000"}}) == ""


class TestPluralRegistersRenderingIntact:
    """The new 'register' branch must not disturb the 'registers' dump."""

    def test_grouped_dump_still_renders(self) -> None:
        data = {
            "categories": [
                {"name": "GPR", "registerNames": ["pc"], "uintValues": [0x088EF0F4]},
            ]
        }
        text = _format_query_text("registers", data)
        assert "── GPR ──" in text
        assert "pc" in text
        assert "0x088EF0F4" in text
