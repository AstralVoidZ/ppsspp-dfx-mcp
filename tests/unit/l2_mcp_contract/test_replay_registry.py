"""L2 MCP contract: ppsspp_replay tool registration.

Anchor: server.py `_TOOL_REGISTRY` Phase 6 entry + `_ANNOTATIONS`
ToolAnnotations for `ppsspp_replay` + TDQS description format
(PURPOSE / USAGE / BEHAVIOR / RETURNS). Phase 6 (OpenSpec change
`add-replay-tools`).

L2 tests are pure metadata: they inspect registration tables that
FastMCP reads to generate `tools/list` responses. They do NOT call the
tool function — that's the job of L1 (forwarding) / L4 (invariants).
"""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp import server as server_mod


# ============================================================================
# Registry membership — ppsspp_replay is registered
# ============================================================================


class TestReplayRegistryMembership:
    """`ppsspp_replay` must be present in _TOOL_REGISTRY (Phase 6)."""

    def test_ppsspp_replay_in_registry(self, tool_registry):
        """ppsspp_replay appears in _TOOL_REGISTRY."""
        names = {name for name, _, _ in tool_registry}
        assert "ppsspp_replay" in names, (
            "ppsspp_replay must be registered (Phase 6) — "
            "if this fails, server.py _TOOL_REGISTRY is missing the entry."
        )

    def test_ppsspp_replay_in_core_tools_is_false(self):
        """ppsspp_replay is NOT a core tool (only health/session/session_list are).

        Sanity check: replay is a feature tool, not liveness-critical.
        Startup validation (_assert_core_tools) does NOT check for it.
        """
        assert "ppsspp_replay" not in server_mod._CORE_TOOLS, (
            "ppsspp_replay should not be in _CORE_TOOLS — "
            "only health/session/session_list are liveness-critical."
        )


# ============================================================================
# ToolAnnotations — STATE-CHANGE semantics
# ============================================================================


class TestReplayToolAnnotations:
    """ppsspp_replay must be annotated STATE-CHANGE.

    Anchor: server.py `_ANNOTATIONS["ppsspp_replay"]` =
        ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                        idempotentHint=False, openWorldHint=False).

    Rationale: replay.begin / abort / execute / time_set mutate
    recording/playback state (STATE-CHANGE), but are not irreversible
    (not DESTRUCTIVE). Repeated calls are NOT idempotent (calling begin
    twice starts a new recording). The tool only interacts with the
    already-connected PPSSPP session (no external world — False).
    """

    def test_replay_has_annotation_entry(self, annotations):
        """_ANNOTATIONS has an entry for ppsspp_replay."""
        assert "ppsspp_replay" in annotations, (
            "_ANNOTATIONS must have an entry for ppsspp_replay."
        )

    def test_replay_read_only_hint_false(self, annotations):
        """readOnlyHint=False — replay mutates recording/playback state."""
        ann = annotations["ppsspp_replay"]
        assert ann.read_only_hint is False, (
            f"ppsspp_replay readOnlyHint should be False, "
            f"got {ann.read_only_hint!r}"
        )

    def test_replay_destructive_hint_false(self, annotations):
        """destructiveHint=False — replay is not irreversible."""
        ann = annotations["ppsspp_replay"]
        assert ann.destructive_hint is False, (
            f"ppsspp_replay destructiveHint should be False, "
            f"got {ann.destructive_hint!r}"
        )

    def test_replay_idempotent_hint_false(self, annotations):
        """idempotentHint=False — calling begin twice starts a new recording."""
        ann = annotations["ppsspp_replay"]
        assert ann.idempotent_hint is False, (
            f"ppsspp_replay idempotentHint should be False, "
            f"got {ann.idempotent_hint!r}"
        )

    def test_replay_open_world_hint_false(self, annotations):
        """openWorldHint=False — tool only talks to the connected PPSSPP session."""
        ann = annotations["ppsspp_replay"]
        assert ann.open_world_hint is False, (
            f"ppsspp_replay openWorldHint should be False, "
            f"got {ann.open_world_hint!r}"
        )


# ============================================================================
# TDQS description format
# ============================================================================


class TestReplayDescriptionTdqs:
    """ppsspp_replay description follows TDQS 4-paragraph format.

    Anchor: specs/tdqs-descriptions/spec.md — PURPOSE / USAGE / BEHAVIOR /
    RETURNS, in that order. Each header is an uppercase keyword followed
    by a colon.
    """

    _TDQS_HEADERS = ("PURPOSE:", "USAGE:", "BEHAVIOR:", "RETURNS:")

    @pytest.mark.parametrize("header", _TDQS_HEADERS)
    def test_each_tdqs_header_present(self, tool_registry, header):
        """ppsspp_replay description contains all 4 TDQS headers."""
        for name, desc, _ in tool_registry:
            if name == "ppsspp_replay":
                assert header in desc, (
                    f"ppsspp_replay: missing TDQS header {header!r}"
                )
                return
        pytest.fail("ppsspp_replay not found in registry")

    def test_replay_tdqs_headers_in_order(self, tool_registry):
        """TDQS headers appear in PURPOSE → USAGE → BEHAVIOR → RETURNS order."""
        for name, desc, _ in tool_registry:
            if name == "ppsspp_replay":
                positions = [desc.find(h) for h in self._TDQS_HEADERS]
                assert all(p >= 0 for p in positions), (
                    "ppsspp_replay: missing at least one TDQS header"
                )
                assert positions == sorted(positions), (
                    f"ppsspp_replay: TDQS headers out of order: {positions}"
                )
                return
        pytest.fail("ppsspp_replay not found in registry")

    def test_behavior_keyword_is_state_change(self, tool_registry):
        """ppsspp_replay BEHAVIOR paragraph starts with 'STATE-CHANGE.'.

        Anchor: server.py description —
            "BEHAVIOR: STATE-CHANGE. Most actions send a synchronous RPC ..."
        All 4 keywords are: DESTRUCTIVE. / MUTATING. / READ-ONLY. /
        STATE-CHANGE. The replay tool mutates recording state but is
        not irreversible, so STATE-CHANGE is correct.

        The canonical form is "BEHAVIOR: <KEYWORD>. ...". We extract
        the BEHAVIOR paragraph (between the BEHAVIOR: header and the
        next \\n\\n separator) and assert it starts with the STATE-CHANGE
        keyword — locking in the trailing-keyword contract.
        """
        for name, desc, _ in tool_registry:
            if name == "ppsspp_replay":
                behavior_idx = desc.find("BEHAVIOR:")
                assert behavior_idx >= 0, (
                    "ppsspp_replay: missing BEHAVIOR: header"
                )
                # BEHAVIOR paragraph spans from the header to the next
                # \n\n separator (or end of string).
                next_para = desc.find("\n\n", behavior_idx)
                if next_para < 0:
                    next_para = len(desc)
                behavior_para = desc[behavior_idx:next_para]
                # Canonical form: "BEHAVIOR: STATE-CHANGE. ..." — the
                # keyword must appear right after the header colon.
                assert "BEHAVIOR: STATE-CHANGE." in behavior_para, (
                    f"ppsspp_replay: BEHAVIOR paragraph must start with "
                    f"'BEHAVIOR: STATE-CHANGE.' — paragraph: "
                    f"{behavior_para[:80]!r}"
                )
                return
        pytest.fail("ppsspp_replay not found in registry")

    def test_description_mentions_all_actions(self, tool_registry):
        """Description USAGE mentions all 10 replay actions.

        Locks in that the schema advertises every supported action —
        hiding an action would silently break callers that depend on it.
        """
        expected_actions = (
            "begin", "abort", "flush", "execute", "status",
            "time_get", "time_set", "save", "load", "wait_complete",
        )
        for name, desc, _ in tool_registry:
            if name == "ppsspp_replay":
                for action in expected_actions:
                    assert action in desc, (
                        f"ppsspp_replay description must mention "
                        f"action={action!r}"
                    )
                return
        pytest.fail("ppsspp_replay not found in registry")
