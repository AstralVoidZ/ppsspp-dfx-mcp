"""L4 regression invariants for the replay subsystem.

Locks in non-obvious implementation choices that, if reverted silently,
would break callers or hide bugs:

- R-01: replay ↔ replay tool parameter sets match (no param loss
  across the dispatch wrapper)
- R-02: replay_execute requires both `version` and `base64` (no defaults
  on the DebugClient method — PPSSPP needs both)
- R-03: replay_wait_complete defaults timeout_ms=10000 / interval_ms=100
  (spike U3 contract)
- R-04: replay tool validates action before touching session_client
  (so action validation is testable without a live session)
- R-05: save / load actions raise ToolError code=NOT_IMPLEMENTED
  (P1 placeholders, advertized in TDQS USAGE)
- R-06: execute action requires version != 0 AND base64_input != ""
  (P0 contract; silently sending empty data would corrupt PPSSPP state)
- R-07: ReplayResult is a frozen dataclass (callers can't mutate it
  in place — pipeflow contract)
- R-08: ReplayResponse.from_result copies every ReplayResult field
  (no silent field dropping in the view layer)
- R-09: replay tool computes flush size client-side from the base64
  payload (PPSSPP's replay.flush response has no `size` field). Locked
  in via behavior: mock flush response → assert result.size equals the
  decoded byte length, not the raw base64 length, and not any bogus
  `size` value that might appear in the response.
- R-10: replay tool extracts `_wait_iterations` from the wait_complete
  response into the dedicated `wait_iterations` field, and keeps it out
  of the public `data` dict. Locked in via behavior: mock the
  DebugClient-injected `_wait_iterations` field → assert it surfaces in
  `wait_iterations`, does NOT leak into `data`, and that the original
  response dict is not mutated.

Anchor: spike evidence in
docs/experiment/experiment_ppsspp_replay_spike_v1.md (U1-U3) and
docs/experiment/experiment_state_probe_running_v1.md (U4). Phase 6
(OpenSpec change `add-replay-tools`).
"""

from __future__ import annotations

import base64 as _b64
import dataclasses
import inspect
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.errors import ToolError
from ppsspp_dfx_mcp.models.replay import ReplayResult
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient
from ppsspp_dfx_mcp.tools.replay import replay
from ppsspp_dfx_mcp.tools.replay import replay
from ppsspp_dfx_mcp.views.replay import ReplayResponse


def _patch_session_client(monkeypatch: pytest.MonkeyPatch, mock_client: AsyncMock):
    """Patch tools.replay.session_client to yield mock_client.

    Mirrors the helper in test_replay_p1_file_io.py — keeps R-09 / R-10
    behavior tests independent of the real session_manager + WsTransport
    stack (no live PPSSPP required).
    """

    @asynccontextmanager
    async def fake_session_client(session_id: str) -> AsyncIterator[AsyncMock]:
        yield mock_client

    monkeypatch.setattr(
        "ppsspp_dfx_mcp.tools.replay.session_client",
        fake_session_client,
    )


# ============================================================================
# R-01: replay ↔ replay tool parameter sets match
# ============================================================================


class TestReplayParamSetConsistency:
    """replay wrapper and replay tool share the same parameter set.

    Anchor: tools/replay.py:replay.
    The wrapper passes every kwarg by name, so a missing or renamed
    param in either direction would silently drop the value.
    """

    def test_both_have_session_id(self):
        async_sig = inspect.signature(replay)
        tool_sig = inspect.signature(replay)
        assert "session_id" in async_sig.parameters
        assert "session_id" in tool_sig.parameters

    def test_both_have_action(self):
        async_sig = inspect.signature(replay)
        tool_sig = inspect.signature(replay)
        assert "action" in async_sig.parameters
        assert "action" in tool_sig.parameters

    def test_replay_param_sets_match(self):
        """L4 anchor: both wrappers expose the same parameter names."""
        async_sig = inspect.signature(replay)
        tool_sig = inspect.signature(replay)
        async_params = set(async_sig.parameters.keys())
        tool_params = set(tool_sig.parameters.keys())
        assert async_params == tool_params, (
            f"replay params {async_params} != replay params "
            f"{tool_params} — every param must be forwarded."
        )


# ============================================================================
# R-02: replay_execute requires both version + base64 (no defaults)
# ============================================================================


class TestReplayExecuteNoDefaults:
    """replay_execute(version, base64) has no defaults on either param.

    Anchor: service/debug_client.py:replay_execute — both `version` and
    `base64` are required positional args. PPSSPP ReplaySubscriber.cpp
    requires both — silently defaulting to 0 / "" would cause a
    "version mismatch" or empty-replay error from PPSSPP.
    """

    def test_version_has_no_default(self):
        sig = inspect.signature(PpssppDebugClient.replay_execute)
        params = sig.parameters
        assert "version" in params
        assert params["version"].default is inspect.Parameter.empty, (
            "replay_execute `version` must be required (no default) — "
            "PPSSPP requires both version + base64 to start playback."
        )

    def test_base64_has_no_default(self):
        sig = inspect.signature(PpssppDebugClient.replay_execute)
        params = sig.parameters
        assert "base64" in params
        assert params["base64"].default is inspect.Parameter.empty, (
            "replay_execute `base64` must be required (no default) — "
            "PPSSPP requires both version + base64 to start playback."
        )


# ============================================================================
# R-03: replay_wait_complete defaults (spike U3 contract)
# ============================================================================


class TestReplayWaitCompleteDefaults:
    """replay_wait_complete defaults timeout_ms=10000 / interval_ms=100.

    Anchor: spike U3 (experiment_ppsspp_replay_spike_v1.md) — replay.execute
    does not auto-end, so the poller's defaults must be generous enough
    for typical replays (10s total) without burning CPU (100ms interval).
    """

    def test_default_timeout_ms_is_10000(self):
        sig = inspect.signature(PpssppDebugClient.replay_wait_complete)
        assert sig.parameters["timeout_ms"].default == 10000, (
            f"replay_wait_complete timeout_ms default should be 10000, "
            f"got {sig.parameters['timeout_ms'].default}"
        )

    def test_default_interval_ms_is_100(self):
        sig = inspect.signature(PpssppDebugClient.replay_wait_complete)
        assert sig.parameters["interval_ms"].default == 100, (
            f"replay_wait_complete interval_ms default should be 100, "
            f"got {sig.parameters['interval_ms'].default}"
        )


# ============================================================================
# R-04 / R-05 / R-06: action validation (no live session needed)
# ============================================================================


class TestReplayActionValidation:
    """replay tool validates action before touching session_client.

    The validation block at the top of `replay()` runs BEFORE the
    `async with session_client(session_id) as client:` line, so we can
    invoke the tool with a dummy session_id and assert the ToolError
    without setting up a live session.
    """

    @pytest.mark.asyncio
    async def test_invalid_action_raises_internal(self):
        """R-04: unknown action raises ToolError code=INTERNAL."""
        with pytest.raises(ToolError, match="invalid action") as exc_info:
            await replay(session_id="dummy", action="bogus")
        assert exc_info.value.code == "INTERNAL"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["save", "load"])
    async def test_save_load_require_file_path(self, action):
        """R-05: save / load require file_path (P1 file I/O invariant).

        Anchor: TDQS USAGE — "action=save requires file_path" /
        "action=load requires file_path". P0 raised NOT_IMPLEMENTED;
        P1 implements save/load but enforces file_path presence.
        """
        with pytest.raises(ToolError, match="file_path is required") as exc_info:
            await replay(session_id="dummy", action=action)
        assert exc_info.value.code == "INTERNAL"

    @pytest.mark.asyncio
    async def test_execute_with_zero_version_raises_internal(self):
        """R-06a: execute action requires version != 0.

        Anchor: tools/replay.py — `if version == 0: raise ToolError(...)`.
        Locking this in catches a regression where version defaults to 0
        and silently sends an empty version to PPSSPP.
        """
        with pytest.raises(ToolError, match="version is required") as exc_info:
            await replay(
                session_id="dummy",
                action="execute",
                version=0,
                base64_input="AAEC",
            )
        assert exc_info.value.code == "INTERNAL"

    @pytest.mark.asyncio
    async def test_execute_with_empty_base64_raises_internal(self):
        """R-06b: execute action requires non-empty base64_input.

        Anchor: tools/replay.py — `if not base64_input: raise ToolError(...)`.
        Locking this in catches a regression where an empty payload is
        silently sent to PPSSPP (causing a 'Game not running' or
        decoding error deep in PPSSPP).
        """
        with pytest.raises(ToolError, match="base64_input is required") as exc_info:
            await replay(
                session_id="dummy",
                action="execute",
                version=1,
                base64_input="",
            )
        assert exc_info.value.code == "INTERNAL"


# ============================================================================
# R-07: ReplayResult is a frozen dataclass
# ============================================================================


class TestReplayResultFrozen:
    """ReplayResult must be a frozen dataclass.

    Anchor: models/replay.py — `@dataclass(frozen=True)`. Frozen-ness
    is part of the pipeflow contract: models are immutable values passed
    between layers. A regression here would let a tool mutate the
    result in place and break idempotency assumptions.
    """

    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(ReplayResult)

    def test_is_frozen(self):
        """Frozen dataclass raises FrozenInstanceError on assignment."""
        r = ReplayResult(action="status")
        with pytest.raises(dataclasses.FrozenInstanceError):
            r.action = "begin"  # type: ignore[misc]

    def test_default_action_is_status(self):
        """Default action='status' (matches a no-op query).

        Locking this in catches a regression where the default changes
        to a mutating action (e.g. 'begin'), which would surprise
        callers that construct a default and then override only one
        field.
        """
        r = ReplayResult()
        assert r.action == "status"
        assert r.executing is False
        assert r.saving is False
        assert r.version == 0
        assert r.size == 0
        assert r.base64 == ""
        assert r.base_rtc == 0
        assert r.data is None
        assert r.wait_iterations == 0


# ============================================================================
# R-08: ReplayResponse.from_result copies every field
# ============================================================================


class TestReplayResponseFromResult:
    """ReplayResponse.from_result must copy every ReplayResult field.

    Anchor: views/replay.py:ReplayResponse.from_result. A regression
    that drops a field would silently hide data from the MCP client.
    """

    def test_all_fields_copied(self):
        """Every ReplayResult field appears in the resulting ReplayResponse."""
        result = ReplayResult(
            action="flush",
            executing=False,
            saving=True,
            version=1,
            size=42,
            base64="AAEC",
            base_rtc=1700000000,
            data={"raw": "echo"},
            wait_iterations=3,
        )
        response = ReplayResponse.from_result(result)
        assert response.action == "flush"
        assert response.executing is False
        assert response.saving is True
        assert response.version == 1
        assert response.size == 42
        assert response.base64 == "AAEC"
        assert response.base_rtc == 1700000000
        assert response.data == {"raw": "echo"}
        assert response.wait_iterations == 3

    def test_none_data_becomes_empty_dict(self):
        """ReplayResult.data=None becomes {} in the view (Pydantic contract).

        Locking in the `dict(result.data) if result.data else {}` branch
        in from_result — a regression that passes None through would
        violate the FrozenModel schema (data: dict[str, Any] is required).
        """
        result = ReplayResult(action="begin", data=None)
        response = ReplayResponse.from_result(result)
        assert response.data == {}


# ============================================================================
# R-09: replay tool computes flush size client-side (behavior test)
# ============================================================================


class TestReplayFlushSizeComputation:
    """replay tool computes flush size client-side from the base64 payload,
    not from a PPSSPP-reported `size` field.

    Anchor: tools/replay.py:_compute_base64_size — flush branch:
        size = _compute_base64_size(b64)
    PPSSPP's replay.flush response only contains {version, base64}; it
    does NOT report a `size` field. Computing it client-side is the
    only option. A regression that reads `resp.get("size")` would
    silently report 0 for every flush.

    These tests lock in the behavior via mock responses rather than
    source-string matching, so refactors (e.g. extracting a helper) do
    not require touching the L4 invariants.
    """

    @pytest.mark.asyncio
    async def test_flush_size_equals_decoded_byte_length(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """R-09a: result.size == len(b64decode(base64)).

        "AAEC" decodes to 3 bytes (0x00 0x01 0x02); the raw base64
        string is 4 chars. A regression that uses len(base64_str)
        would report 4; the correct decoded length is 3.
        """
        mock = AsyncMock()
        mock.replay_flush.return_value = {"version": 1, "base64": "AAEC"}
        _patch_session_client(monkeypatch, mock)

        result = await replay(session_id="sess-1", action="flush")

        assert result["action"] == "flush"
        assert result["version"] == 1
        assert result["base64"] == "AAEC"
        assert result["size"] == 3
        assert result["size"] != len("AAEC")  # 3 != 4

    @pytest.mark.asyncio
    async def test_flush_ignores_size_field_in_response(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """R-09b: a bogus `size` in the response must NOT be trusted.

        PPSSPP's replay.flush response has no `size` field, but a
        regression that reads `resp.get("size")` would silently return
        whatever value appears there. Lock in that size is always
        computed client-side by injecting a bogus `size` and asserting
        the computed value wins.
        """
        mock = AsyncMock()
        mock.replay_flush.return_value = {
            "version": 1,
            "base64": "AAEC",  # decodes to 3 bytes
            "size": 9999,  # bogus — must be ignored
        }
        _patch_session_client(monkeypatch, mock)

        result = await replay(session_id="sess-1", action="flush")
        assert result["size"] == 3
        assert result["size"] != 9999

    @pytest.mark.asyncio
    async def test_flush_empty_base64_has_zero_size(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """R-09c: empty base64 string yields size=0 (no crash)."""
        mock = AsyncMock()
        mock.replay_flush.return_value = {"version": 0, "base64": ""}
        _patch_session_client(monkeypatch, mock)

        result = await replay(session_id="sess-1", action="flush")
        assert result["size"] == 0
        assert result["base64"] == ""

    @pytest.mark.asyncio
    async def test_flush_invalid_base64_has_zero_size(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """R-09d: malformed base64 yields size=0 (no exception).

        Locks in the error-tolerant sizing policy centralized in
        _compute_base64_size: a corrupted flush response must not crash
        the tool — it surfaces size=0 with the raw base64 still in the
        result for diagnosis.
        """
        mock = AsyncMock()
        mock.replay_flush.return_value = {
            "version": 1,
            "base64": "!!!not-base64!!!",
        }
        _patch_session_client(monkeypatch, mock)

        result = await replay(session_id="sess-1", action="flush")
        assert result["size"] == 0
        assert result["base64"] == "!!!not-base64!!!"

    @pytest.mark.asyncio
    async def test_flush_size_matches_real_spike_data(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """R-09e: flush size matches real spike U1 recording length.

        Anchor: experiment_ppsspp_replay_spike_v1.md U1 — the 51-byte
        recording. Locks in that the decoded length matches the spike
        evidence (catches a regression where size is computed via a
        different code path that disagrees with b64decode).
        """
        spike_b64 = (
            "ACycRgEAAAAAAEAAAAAAAAQBLJxGAQAAAACAgICAAAAABABExk8BAAAAAAAAAAB/AADQ"
        )
        expected = len(_b64.b64decode(spike_b64))
        mock = AsyncMock()
        mock.replay_flush.return_value = {"version": 1, "base64": spike_b64}
        _patch_session_client(monkeypatch, mock)

        result = await replay(session_id="sess-1", action="flush")
        assert result["size"] == expected


# ============================================================================
# R-10: replay tool extracts _wait_iterations (behavior test)
# ============================================================================


class TestReplayWaitIterationsExtraction:
    """replay tool extracts `_wait_iterations` from the wait_complete
    response into the dedicated `wait_iterations` field, and keeps it
    out of the public `data` dict.

    Anchor: tools/replay.py — wait_complete branch:
        iterations = int(resp.get("_wait_iterations", 0))
        clean_data = {k: v for k, v in resp.items() if k != "_wait_iterations"}
    The `_wait_iterations` field is injected by DebugClient (not by
    PPSSPP) — it must NOT leak into the public `data` field of the
    response. A regression that forwards it would expose an internal
    counter to MCP clients.

    These tests lock in the behavior via mock responses rather than
    source-string matching, so refactors (e.g. switching from pop to a
    dict comprehension) do not require touching the L4 invariants.
    """

    @pytest.mark.asyncio
    async def test_wait_iterations_surfaces_in_dedicated_field(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """R-10a: _wait_iterations surfaces as result.wait_iterations."""
        mock = AsyncMock()
        mock.replay_wait_complete.return_value = {
            "executing": False,
            "saving": False,
            "_wait_iterations": 7,
        }
        _patch_session_client(monkeypatch, mock)

        result = await replay(
            session_id="sess-1",
            action="wait_complete",
            timeout_ms=5000,
            interval_ms=50,
        )

        assert result["action"] == "wait_complete"
        assert result["wait_iterations"] == 7
        # Confirm the mock was called with the right timeout / interval.
        mock.replay_wait_complete.assert_awaited_once_with(
            timeout_ms=5000, interval_ms=50,
        )

    @pytest.mark.asyncio
    async def test_wait_iterations_does_not_leak_into_data(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """R-10b: _wait_iterations must NOT appear in result.data.

        The internal counter is DebugClient-injected (not from PPSSPP)
        and would leak an implementation detail to MCP clients if
        forwarded. result.data should contain only the public fields
        from replay.status (executing / saving / ...).
        """
        mock = AsyncMock()
        mock.replay_wait_complete.return_value = {
            "executing": False,
            "saving": False,
            "_wait_iterations": 3,
        }
        _patch_session_client(monkeypatch, mock)

        result = await replay(session_id="sess-1", action="wait_complete")

        assert "_wait_iterations" not in result["data"]
        # The rest of the response should still be present.
        assert result["data"]["executing"] is False
        assert result["data"]["saving"] is False

    @pytest.mark.asyncio
    async def test_wait_iterations_defaults_to_zero_when_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """R-10c: missing _wait_iterations defaults to 0 (no KeyError).

        A regression that uses `resp["_wait_iterations"]` (without
        .get default) would crash if DebugClient ever returns a
        response without the field (e.g. a future protocol change).
        """
        mock = AsyncMock()
        mock.replay_wait_complete.return_value = {
            "executing": True,
            "saving": False,
            # No _wait_iterations key.
        }
        _patch_session_client(monkeypatch, mock)

        result = await replay(session_id="sess-1", action="wait_complete")
        assert result["wait_iterations"] == 0

    @pytest.mark.asyncio
    async def test_wait_complete_does_not_mutate_response(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """R-10d: wait_complete branch must not mutate the transport's
        response dict.

        A regression that uses `resp.pop("_wait_iterations")` would
        mutate the dict returned by DebugClient.replay_wait_complete,
        which may break callers that retain a reference to the same
        dict (e.g. for logging or retry logic). Lock in the
        non-mutating extraction by holding the original response and
        asserting _wait_iterations is still present after the tool
        returns.
        """
        original_resp: dict[str, Any] = {
            "executing": False,
            "saving": False,
            "_wait_iterations": 5,
        }
        mock = AsyncMock()
        mock.replay_wait_complete.return_value = original_resp
        _patch_session_client(monkeypatch, mock)

        await replay(session_id="sess-1", action="wait_complete")

        # The original response dict must still contain the internal
        # field — the tool must copy/filter, not pop in place.
        assert "_wait_iterations" in original_resp
        assert original_resp["_wait_iterations"] == 5
