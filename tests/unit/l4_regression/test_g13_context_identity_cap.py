"""G-13 (FR-013): context identity must stop attributing far-away addresses.

Deep-test case P2-28 (mcp_test_report/tools/ppsspp_context.md):
ppsspp_context(0x09FFF000) -- an address in no known function -- answered
identity={name: sub_12CAB8, start: 0x08930AB8, offset: 23913800}. The
nearest-symbol match had no distance limit, so any address above the
lowest known function start got "explained" as an offset into some
function -- in crash triage that attribution is misinformation.

Fix: `_MAX_IDENTITY_DISTANCE = 0x1000` (plan.md FR-013: a plausible
function-body size; known_functions carries starts only, no sizes). Past
the cap identity is null and the note still names the nearest symbol for
orientation. The two null cases stay distinct:

  within cap            -> identity resolves (offset = distance)
  beyond cap (near miss) -> identity=null; note names the nearest symbol
                            and the attribution cap
  no candidate at/below  -> identity=null; note keeps the TOPX
                            knowledge-gap text (not a near miss)

Two-way acceptance (spec.md FR-013): the far address must come back null;
an address inside a known function must still resolve.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from ppsspp_dfx_mcp.tools import context as context_mod
from ppsspp_dfx_mcp.tools.context import context

# Fixture space: the P2-28 pair (sub_12CAB8 at 0x08930AB8) plus a lower
# entry, so "nearest at or below" picks different symbols per address.
FUNCS = {"top_entry": 0x08804000, "sub_12CAB8": 0x08930AB8}


class _FakeClient:
    async def memory_map(self):
        return {"ranges": [{"start": 0x08800000, "end": 0x0A800000, "type": "ram"}]}

    async def disasm(self, address, count):
        return [{"address": address, "text": "nop"}]


@asynccontextmanager
async def _fake_session_client(_session_id):
    yield _FakeClient()


def _patch_world(monkeypatch) -> None:
    monkeypatch.setattr(context_mod, "session_client", _fake_session_client)
    monkeypatch.setattr(context_mod, "_known_functions_runtime", lambda: dict(FUNCS))

    async def _resolve(session_id):
        return session_id or "sess-fake"

    monkeypatch.setattr(context_mod, "resolve_session_id", _resolve)


class TestCapValueIsPinned:
    def test_cap_is_positive_and_at_most_4kib(self) -> None:
        """Fails if the cap is removed or raised past the planned 0x1000."""
        assert 0 < context_mod._MAX_IDENTITY_DISTANCE <= 0x1000


class TestMatchIdentityBoundary:
    """The cap boundary itself: == cap resolves, cap+1 is rejected."""

    def test_start_itself_resolves(self) -> None:
        view, miss = context_mod._match_identity(0x08804000, FUNCS)
        assert miss == ""
        assert view is not None
        assert (view.name, view.offset) == ("top_entry", 0)

    def test_offset_exactly_at_cap_still_resolves(self) -> None:
        cap = context_mod._MAX_IDENTITY_DISTANCE
        view, miss = context_mod._match_identity(0x08804000 + cap, FUNCS)
        assert miss == ""
        assert view is not None
        assert (view.name, view.offset) == ("top_entry", cap)

    def test_one_byte_past_cap_is_rejected(self) -> None:
        cap = context_mod._MAX_IDENTITY_DISTANCE
        view, miss = context_mod._match_identity(0x08804000 + cap + 1, FUNCS)
        assert view is None
        assert "top_entry" in miss
        assert f"0x{cap:X}" in miss

    def test_inside_second_function_resolves_to_it(self) -> None:
        view, _ = context_mod._match_identity(0x08930AB8 + 0x40, FUNCS)
        assert view is not None
        assert (view.name, view.offset) == ("sub_12CAB8", 0x40)

    def test_p2_28_address_rejected_with_nearest_named(self) -> None:
        """The deep-test address from the report must no longer resolve."""
        view, miss = context_mod._match_identity(0x09FFF000, FUNCS)
        assert view is None
        assert "sub_12CAB8" in miss
        assert "not in a known function range" in miss

    def test_below_all_starts_is_gap_not_rejection(self) -> None:
        view, miss = context_mod._match_identity(0x087FFFFF, FUNCS)
        assert (view, miss) == (None, "")


class TestTwoWayAcceptanceOnTheTool:
    """spec.md FR-013: far address -> null; in-function address -> resolves."""

    async def test_far_address_returns_null_identity(self, monkeypatch) -> None:
        _patch_world(monkeypatch)
        out = await context(address="0x09FFF000", session_id="sess-fake")
        assert out["identity"] is None
        assert len(out["disasm"]) > 0, "degradation must still return the raw window"

    async def test_far_address_note_names_cap_and_nearest(self, monkeypatch) -> None:
        _patch_world(monkeypatch)
        out = await context(address="0x09FFF000", session_id="sess-fake")
        note = out["backtrace_note"]
        assert "sub_12CAB8" in note, f"nearest symbol lost for orientation: {note}"
        assert "not in a known function range" in note, note
        assert f"0x{context_mod._MAX_IDENTITY_DISTANCE:X}" in note, note
        assert "topx" not in note.lower(), (
            f"a near miss must not be reported as a table/scope gap: {note}"
        )

    async def test_in_function_address_still_resolves(self, monkeypatch) -> None:
        _patch_world(monkeypatch)
        out = await context(address="0x08804100", session_id="sess-fake")
        assert out["identity"]["name"] == "top_entry"
        assert out["identity"]["offset"] == 0x100
        assert "identity unresolved" not in out["backtrace_note"]

    async def test_gap_below_all_starts_keeps_topx_message(self, monkeypatch) -> None:
        """No candidate at all is a knowledge gap, not a near miss."""
        _patch_world(monkeypatch)
        out = await context(address="0x087FFFFF", session_id="sess-fake")
        assert out["identity"] is None
        assert "topx" in out["backtrace_note"].lower()
