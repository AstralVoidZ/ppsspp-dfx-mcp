"""L1 contract tests for replay methods.

Anchors (PPSSPP C++ source — open_source/ppsspp/Core/Debugger/WebSocket/ReplaySubscriber.cpp):
- replay.begin:    no params; begins/resumes recording
- replay.abort:    no params; aborts any recording or execution
- replay.flush:    no params; returns {version, base64}
- replay.execute:  params {version, base64}; starts playback (does NOT auto-end — spike U3)
- replay.status:   no params; returns {executing, saving}
- replay.time.get: no params; returns {value} (base RTC seconds)
- replay.time.set: params {value}; overwrites base RTC
- replay_wait_complete: client-side poller (no WS event of its own);
  polls replay.status until executing=False or timeout (spike U3 fix)

L1 tests assert pure forwarding behavior: each DebugClient.replay_*
method forwards the correct PPSSPP WebSocket event name + parameters,
and passes the response through verbatim (no field extraction — that's
the tool layer's job, exercised in L4 invariants). Uses the canonical
FakeTransport (configured via the `transport` and `client` fixtures in
conftest.py).

Phase 6 (OpenSpec change `add-replay-tools`).
"""

from __future__ import annotations

import asyncio

import pytest


class TestReplayContract:
    """L1 contract: replay methods forward correct event + params."""

    @pytest.mark.asyncio
    async def test_replay_begin_forwards_event(self, client, transport):
        """replay_begin() forwards to `replay.begin` WS event with no params.

        Anchor: ReplaySubscriber.cpp — replay.begin takes no parameters.
        """
        transport.set_response("replay.begin", {"ok": True})
        await client.replay_begin()
        assert transport.calls[-1][0] == "replay.begin"
        assert transport.calls[-1][1] == {}

    @pytest.mark.asyncio
    async def test_replay_abort_forwards_event(self, client, transport):
        """replay_abort() forwards to `replay.abort` WS event with no params.

        Anchor: ReplaySubscriber.cpp — replay.abort takes no parameters.
        """
        transport.set_response("replay.abort", {"ok": True})
        await client.replay_abort()
        assert transport.calls[-1][0] == "replay.abort"
        assert transport.calls[-1][1] == {}

    @pytest.mark.asyncio
    async def test_replay_flush_forwards_event_and_passes_response(
        self, client, transport
    ):
        """replay_flush() forwards to `replay.flush` WS event with no params.

        Anchor: ReplaySubscriber.cpp — replay.flush returns {version, base64}.
        The method passes the response through verbatim — it does NOT
        compute the binary size client-side (that's the tool layer's job).
        """
        transport.set_response(
            "replay.flush",
            {"version": 1, "base64": "AAEC"},
        )
        result = await client.replay_flush()
        assert transport.calls[-1][0] == "replay.flush"
        assert transport.calls[-1][1] == {}
        assert result == {"version": 1, "base64": "AAEC"}

    @pytest.mark.asyncio
    async def test_replay_execute_forwards_version_and_base64(
        self, client, transport
    ):
        """replay_execute(version, base64) forwards both params.

        Anchor: ReplaySubscriber.cpp — replay.execute takes {version, base64}.
        Neither param has a default in the Python method — both are
        required positional args.
        """
        transport.set_response("replay.execute", {"ok": True})
        await client.replay_execute(version=1, base64="AAEC")
        assert transport.calls[-1][0] == "replay.execute"
        assert transport.calls[-1][1] == {"version": 1, "base64": "AAEC"}

    @pytest.mark.asyncio
    async def test_replay_status_forwards_event_and_passes_response(
        self, client, transport
    ):
        """replay_status() forwards to `replay.status` WS event with no params.

        Anchor: ReplaySubscriber.cpp — replay.status returns
        {executing, saving}. The method passes the response through
        verbatim — it does NOT extract individual bool fields (that's
        the tool layer's job, exercised in L4 invariants).
        """
        transport.set_response(
            "replay.status",
            {"executing": False, "saving": True},
        )
        result = await client.replay_status()
        assert transport.calls[-1][0] == "replay.status"
        assert transport.calls[-1][1] == {}
        assert result == {"executing": False, "saving": True}

    @pytest.mark.asyncio
    async def test_replay_time_get_forwards_event_and_passes_response(
        self, client, transport
    ):
        """replay_time_get() forwards to `replay.time.get` WS event with no params.

        Anchor: ReplaySubscriber.cpp — replay.time.get returns {value}
        (base RTC in seconds). The method passes the response through
        verbatim.
        """
        transport.set_response("replay.time.get", {"value": 1700000000})
        result = await client.replay_time_get()
        assert transport.calls[-1][0] == "replay.time.get"
        assert transport.calls[-1][1] == {}
        assert result == {"value": 1700000000}

    @pytest.mark.asyncio
    async def test_replay_time_set_forwards_value(self, client, transport):
        """replay_time_set(value) forwards `value` to `replay.time.set`.

        Anchor: ReplaySubscriber.cpp — replay.time.set takes {value}
        (base RTC seconds, uint32).
        """
        transport.set_response("replay.time.set", {"ok": True})
        await client.replay_time_set(value=1700000000)
        assert transport.calls[-1][0] == "replay.time.set"
        assert transport.calls[-1][1] == {"value": 1700000000}


class TestReplayWaitCompletePoller:
    """L1 contract: replay_wait_complete client-side poller.

    Anchor: spike U3 finding (experiment_ppsspp_replay_spike_v1.md) —
    `replay.execute` does NOT auto-end; the executing flag stays True
    after the replay finishes. The poller is the only way to confirm
    completion.
    """

    @pytest.mark.asyncio
    async def test_wait_complete_polls_until_not_executing(
        self, client, transport
    ):
        """wait_complete polls replay.status until executing=False.

        First poll returns executing=True (still running); second poll
        returns executing=False (done). The loop exits and returns the
        final status dict with `_wait_iterations` set to the poll count.
        """
        call_count = [0]

        def _poll_response(**_params):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"executing": True, "saving": False}
            return {"executing": False, "saving": False}

        transport.set_response("replay.status", _poll_response)
        result = await client.replay_wait_complete(
            timeout_ms=1000, interval_ms=10
        )
        assert result["executing"] is False
        assert result["_wait_iterations"] == 2
        # Two polls total: first still executing, second done.
        replay_status_calls = [
            c for c in transport.calls if c[0] == "replay.status"
        ]
        assert len(replay_status_calls) == 2

    @pytest.mark.asyncio
    async def test_wait_complete_exits_on_first_poll_if_not_executing(
        self, client, transport
    ):
        """wait_complete exits on the first poll if executing=False.

        Locks in that the loop checks executing BEFORE sleeping —
        a regression here would add latency to every successful wait.
        """
        transport.set_response(
            "replay.status", {"executing": False, "saving": False}
        )
        result = await client.replay_wait_complete(
            timeout_ms=1000, interval_ms=10
        )
        assert result["executing"] is False
        assert result["_wait_iterations"] == 1

    @pytest.mark.asyncio
    async def test_wait_complete_raises_timeout_when_executing_stays_true(
        self, client, transport
    ):
        """wait_complete raises asyncio.TimeoutError when executing stays True.

        Locks in spike U3: replay.execute does not auto-end. If the
        caller doesn't poll, the executing flag stays True forever and
        the timeout fires.
        """
        transport.set_response(
            "replay.status", {"executing": True, "saving": False}
        )
        with pytest.raises(asyncio.TimeoutError, match="replay.wait_complete timeout"):
            await client.replay_wait_complete(
                timeout_ms=50, interval_ms=10
            )
