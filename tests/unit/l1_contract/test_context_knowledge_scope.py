"""context must say when its address knowledge does not apply (T062 / D11).

The identity/region fields are derived from a TOPX-specific knowledge base:
`_known_functions_runtime()` (addresses.yaml) and the module layout. For
any other ISO they simply come back empty -- `identity: null`,
`region: ""` -- which reads exactly like "the address resolved to nothing",
and is indistinguishable from a genuinely bad address.

That ambiguity matters: an agent pointed at a non-TOPX game concludes the
address is wrong and starts hunting, when in fact the tool's lookup tables
never had that binary in them. So `context` must state which case it is.

  C1.7a identity/region being empty is reported as a KNOWLEDGE GAP, not
       silently returned
  C1.7b the response says the lookup tables are TOPX-specific
  C1.7c when identity and region DO resolve, no such warning appears
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from ppsspp_dfx_mcp.tools.context import _match_region


class _FakeClient:
    def __init__(self, ranges=None):
        self._ranges = ranges if ranges is not None else []

    async def memory_map(self):
        return {"ranges": self._ranges}

    async def disasm(self, **_k):
        return [{"address": 0x08804000, "text": "nop"}]

    def with_stepping(self):
        @asynccontextmanager
        async def _cm():
            yield self

        return _cm()

    async def backtrace(self):
        return {"frames": []}


def _stub(client):
    @asynccontextmanager
    async def _cm(_sid):
        yield client

    return patch("ppsspp_dfx_mcp.tools.context.session_client", _cm)


RANGES = [
    {"type": "ram", "name": "User Memory", "address": "0x08800000", "size": 25165824},
    {"type": "vram", "name": "VRAM", "address": "0x04000000", "size": 2097152},
]


class TestRegionStillWorks:
    """Existing behaviour must not regress."""

    def test_match_region_resolves(self) -> None:
        assert _match_region(0x08804000, RANGES) == "ram"
        assert _match_region(0x04000000, RANGES) == "vram"

    def test_unknown_address_gives_empty(self) -> None:
        assert _match_region(0x20000000, RANGES) == ""


class TestKnowledgeGapIsDeclared:
    """C1.7a / C1.7b"""

    @pytest.mark.asyncio
    async def test_empty_identity_reports_unapplicable(self, client, transport) -> None:
        """A non-TOPX image must not look like a bad address."""
        from ppsspp_dfx_mcp.tools.context import context

        transport.set_response("memory.read", {"data": ""})
        with (
            _stub(_FakeClient(RANGES)),
            # (view, miss_reason): (None, "") = no candidate at all, i.e. a
            # knowledge gap -- the case this test is about (G-13 rejection
            # returns a non-empty reason instead).
            patch("ppsspp_dfx_mcp.tools.context._match_identity", return_value=(None, "")),
            patch(
                "ppsspp_dfx_mcp.tools.context._known_functions_runtime",
                return_value={0x08804000: "top_entry"},
            ),
        ):
            out = await context(session_id="s", address="0x20000000")
        # the address resolved to a region but no known function:
        # say so, rather than leaving identity null with no explanation
        assert out["identity"] is None
        joined = " ".join(str(v) for v in out.values()).lower()
        assert "topx" in joined or "not applicable" in joined or "unknown" in joined, (
            f"no signal that the lookup tables do not cover this binary: {out}"
        )

    @pytest.mark.asyncio
    async def test_missing_region_is_declared(self, client, transport) -> None:
        """region='' must be distinguishable from a resolved region."""
        from ppsspp_dfx_mcp.tools.context import context

        transport.set_response("memory.read", {"data": ""})
        with (
            _stub(_FakeClient([])),
            patch("ppsspp_dfx_mcp.tools.context._match_identity", return_value=(None, "")),
        ):
            out = await context(session_id="s", address="0x20000000")
        assert out["region"] == ""
        joined = " ".join(str(v) for v in out.values()).lower()
        assert "topx" in joined or "not applicable" in joined or "unknown" in joined, (
            f"an empty region gave the caller nothing to act on: {out}"
        )

    @pytest.mark.asyncio
    async def test_resolved_identity_has_no_false_alarm(self, client, transport) -> None:
        """C1.7c -- do not cry wolf when the lookup succeeded."""
        from ppsspp_dfx_mcp.tools.context import context
        from ppsspp_dfx_mcp.views.context import IdentityView

        transport.set_response("memory.read", {"data": ""})
        ident = IdentityView(name="top_entry", start="0x08804000", offset=0)
        with (
            _stub(_FakeClient(RANGES)),
            patch("ppsspp_dfx_mcp.tools.context._match_identity", return_value=(ident, "")),
        ):
            out = await context(session_id="s", address="0x08804000")
        assert out["identity"]["name"] == "top_entry"
        assert out["region"] == "ram"
        joined = " ".join(str(v) for v in out.values()).lower()
        assert "not applicable" not in joined, f"a successful lookup still warned: {out}"


class TestSchemaCarriesTheCaveat:
    """C1.7b -- the tool description is where a caller looks first."""

    def test_description_states_scope(self) -> None:
        """The description is where a caller looks first."""
        import asyncio as _a

        from ppsspp_dfx_mcp import server as srv

        srv.register_all_tools()
        tools = {t.name: t for t in _a.run(srv.mcp.list_tools())}
        d = (tools["ppsspp_context"].description or "").lower()
        assert "topx" in d, (
            "the description must state the address knowledge is TOPX-specific; "
            "otherwise a caller cannot tell an empty identity from a gap"
        )


# Review-v4 W-2: PPSSPP `memory.mapping` returns decimal INTs for address
# bounds (views/memory_map.py:77). The fixture above uses "0x..." strings —
# the wire shape is the one below, and `_match_region` must handle both.
PRODUCTION_SHAPED_RANGES = [
    {"type": "ram", "name": "User Memory", "address": 0x08800000, "size": 25165824},
    {"type": "vram", "name": "VRAM", "address": 0x04000000, "size": 2097152},
]


class TestMatchRegionWireShape:
    def test_match_region_decimal_int_addresses(self) -> None:
        """Wire-shaped ranges (decimal ints) must resolve, not fall through.

        Before the W-2 fix, `int(str(134217728), 16)` parsed the decimal int
        as hex (≈5.15e9), so no PSP address ever matched and `region` came
        back "" with a misleading "outside every reported memory range" note
        on every real session.
        """
        assert _match_region(0x08804000, PRODUCTION_SHAPED_RANGES) == "ram"
        assert _match_region(0x04000000, PRODUCTION_SHAPED_RANGES) == "vram"
        assert _match_region(0x20000000, PRODUCTION_SHAPED_RANGES) == ""

    def test_match_region_mixed_shapes(self) -> None:
        """Ints, 0x-strings and decimal strings must all resolve."""
        mixed = [
            {"type": "a", "start": 0x08800000, "end": 0x0A800000},
            {"type": "b", "start": "0x04000000", "size": "2097152"},
            {"type": "c", "start": "83886080", "size": 1024},
        ]
        assert _match_region(0x088EF0F4, mixed) == "a"
        assert _match_region(0x04000000, mixed) == "b"
        assert _match_region(0x05000010, mixed) == "c"
