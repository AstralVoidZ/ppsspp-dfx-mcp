"""G-10 (FR-010): the 0 value of wait_frames.frames / press_button.duration.

Deep-test cases (mcp_test_report/tools/ppsspp_wait_frames.md and
ppsspp_press_button.md): neither parameter's schema description said what 0
means. A caller reading only the tool surface could not tell a silent no-op
from a meaningful wait/press:

  ppsspp_wait_frames(frames=0)    -> elapsed_s=0.0, emulator not advanced
  ppsspp_press_button(duration=0) -> accepted, no frames held

Fix (spec.md FR-010): BOTH semantics are now declared in the schema
descriptions, and the one that has no meaning is rejected:

  wait_frames.frames    MUST be >= 1 — frames=0 raises ARGS_INVALID
                        ("wait N frames" with N=0 waits for nothing)
  press_button.duration 0 is ACCEPTED and documented as a no-op (press and
                        release inside one frame is a legitimate input)

The frames>=1 rule is enforced in the wait_frames tool body, NOT in the
shared wait_frames_chunked waiter: batch_step wait steps legitimately accept
frames=0 (probe P1.bs.zero_frame_120 runs 120 zero-frame waits, and
core/batch_jobs.py estimates wait cost for frames >= 0). Rejecting 0 in the
shared waiter would desync batch_step's submit-time validation from its
execution.

The old R12 wording "frames=0 is legal" described the TOOL; G-10 split the
two surfaces. verify_real_mcp's B.wait_frames.zero scenario was flipped from
"ok" to "error" in the same change (the real-device gate is not under pytest,
so only operating it surfaced the stale expectation).

Two-way acceptance: frames=0 must fail AND frames=1 must still work (a fix
that broke every wait would pass a "0 raises" test alone); duration=0 must be
accepted (two-way: duration=-1 rejected, duration=0 no-op); and both
descriptions must carry the 0-value wording.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.tools.input import press_button as tool_press_button
from ppsspp_dfx_mcp.tools.input import wait_frames as tool_wait_frames


@pytest.fixture(autouse=True)
def _live_session(monkeypatch):
    """wait_frames validates session liveness before sleeping; stub it out."""
    from ppsspp_dfx_mcp.session import client_helper

    async def _alive(_session_id: str) -> None:
        return None

    monkeypatch.setattr(client_helper, "validate_session_alive", _alive)


class _FakeClient:
    """Minimal debug client: records the press payload it was handed."""

    def __init__(self) -> None:
        self.presses: list[dict[str, Any]] = []

    async def press_button(self, button: str, duration: int) -> dict[str, Any]:
        self.presses.append({"button": button, "duration": duration})
        return {"ok": True}


@pytest.fixture
def fake_client(monkeypatch):
    """Patch session_client in tools.input with a recording fake."""
    client = _FakeClient()

    @asynccontextmanager
    async def _patched(_session_id: str):
        yield client

    monkeypatch.setattr("ppsspp_dfx_mcp.tools.input.session_client", _patched)
    return client


def _tool_input_schema(tool_name: str) -> dict:
    """The registered tool's inputSchema — the text an MCP client sees."""
    from ppsspp_dfx_mcp import server as server_mod

    server_mod.register_all_tools()
    tools = asyncio.run(server_mod.mcp.list_tools())
    return next(t for t in tools if t.name == tool_name).input_schema


def _param_description(tool_name: str, param: str) -> str:
    """Description of one parameter as exposed on the MCP tool surface.

    Read from the live registry (the same text an MCP client sees), not off
    the Python annotation: tools/input.py uses
    ``from __future__ import annotations``, so the local FieldInfo metadata
    is not reachable through ``inspect`` on the raw signature.
    """
    return str(
        (_tool_input_schema(tool_name).get("properties") or {})
        .get(param, {})
        .get("description", "")
    )


class TestWaitFramesRejectsZero:
    """wait_frames.frames=0 must raise ARGS_INVALID, not no-op."""

    async def test_frames_zero_raises_args_invalid(self) -> None:
        with pytest.raises(ArgsInvalid, match="frames must be >= 1"):
            await tool_wait_frames(session_id="s", frames=0)

    async def test_frames_zero_is_not_a_silent_noop(self) -> None:
        """Fails if frames=0 returns a result dict instead of raising."""
        outcome: str
        try:
            await tool_wait_frames(session_id="s", frames=0)
        except ArgsInvalid:
            outcome = "rejected"
        else:
            outcome = "accepted"
        assert outcome == "rejected", (
            "wait_frames(frames=0) must be rejected — accepting it makes an "
            "empty wait indistinguishable from a real one (G-10)"
        )

    async def test_frames_bool_rejected(self) -> None:
        """frames=True is int 1 in Python — the guard must reject it."""
        with pytest.raises(ArgsInvalid, match="frames must be an int"):
            await tool_wait_frames(session_id="s", frames=True)  # type: ignore[arg-type]


class TestWaitFramesStillAcceptsOne:
    """Two-way guard: the fix must not break the smallest legal wait."""

    async def test_frames_one_succeeds(self) -> None:
        result = await tool_wait_frames(session_id="s", frames=1, interval=0.0001)
        assert result["frames"] == 1
        assert result["elapsed_s"] >= 0.0


class TestPressButtonZeroDurationIsNoOp:
    """duration=0 stays ACCEPTED and is forwarded verbatim (R12/P1 probe)."""

    async def test_duration_zero_accepted(self, fake_client) -> None:
        result = await tool_press_button(session_id="s", button="cross", duration=0)
        assert fake_client.presses == [{"button": "cross", "duration": 0}]
        assert result["duration"] == 0
        assert result["button"] == "cross"

    async def test_duration_zero_not_mistaken_for_default(self, fake_client) -> None:
        """Fails if duration=0 is coerced to the default 1."""
        await tool_press_button(session_id="s", button="cross", duration=0)
        assert fake_client.presses[0]["duration"] == 0

    async def test_negative_duration_still_rejected(self, fake_client) -> None:
        with pytest.raises(ArgsInvalid, match="duration must be >= 0"):
            await tool_press_button(session_id="s", button="cross", duration=-1)
        assert fake_client.presses == []


class TestZeroSemanticsAreInTheSchema:
    """FR-010 requires the 0 semantics in the description, not just enforced."""

    def test_wait_frames_description_declares_frames_zero_rejected(self) -> None:
        desc = _param_description("ppsspp_wait_frames", "frames")
        assert ">= 1" in desc, f"wait_frames.frames description must state >= 1: {desc!r}"
        assert "frames=0 is rejected" in desc

    def test_press_button_description_declares_duration_zero_noop(self) -> None:
        desc = _param_description("ppsspp_press_button", "duration")
        assert "duration=0" in desc, (
            f"press_button.duration description must state the 0 semantics: {desc!r}"
        )
        assert "no-op" in desc


class TestBatchStepZeroFrameWaitUnaffected:
    """The shared waiter must KEEP accepting 0 for batch_step wait steps.

    Fails if someone 'fixes' G-10 by tightening wait_frames_chunked, which
    would reject the R12-ratified zero-frame batch wait.
    """

    async def test_shared_waiter_accepts_zero(self) -> None:
        from ppsspp_dfx_mcp.tools._common import wait_frames_chunked

        elapsed = await wait_frames_chunked(0, 0.0001)
        assert elapsed >= 0.0
