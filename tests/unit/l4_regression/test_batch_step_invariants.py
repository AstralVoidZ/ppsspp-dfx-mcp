"""L4 regression tests for batch_step tool invariants.

Locks in:
- Empty steps list raises ToolError
- Step dict must have 'type' field
- Step type must be one of {press, wait, state_probe, screenshot}
- press requires button (valid name) + optional duration (>=0)
- wait requires frames (int >=0)
- state_probe accepts optional names / samples
- Recording-mode screenshot auto-skip (spike U2 invariant)
- on_failure='abort' stops batch on first failure
- on_failure='continue' keeps running after failure
- press / wait / state_probe route to the right client method

Uses monkeypatch to mock session_client. replay_status is mocked on
the same client to control recording_mode detection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.errors import ToolError
from ppsspp_dfx_mcp.tools import state_observer as so_module
from ppsspp_dfx_mcp.tools.batch_step import batch_step


async def _async_noop(session_id: str) -> None:
    """Stand-in for client_helper.validate_session_alive (W3 waiter)."""
    return None


def _reset_registry() -> None:
    """Clear state_observer module-level registry (test isolation)."""
    so_module._REGISTRY.clear()
    so_module._SEEDED = False


def _patch_client(monkeypatch: pytest.MonkeyPatch, mock: AsyncMock) -> None:
    """Patch session_client to yield mock (single shared client)."""

    @asynccontextmanager
    async def fake_session_client(session_id: str) -> AsyncIterator[AsyncMock]:
        yield mock

    monkeypatch.setattr(
        "ppsspp_dfx_mcp.tools.batch_step.session_client",
        fake_session_client,
    )
    # state_observer (called via delegation from batch_step) also needs
    # patching — but it imports session_client at call time, so patching
    # the same symbol on its module is sufficient.
    monkeypatch.setattr(
        "ppsspp_dfx_mcp.tools.state_observer.session_client",
        fake_session_client,
    )


def _make_mock_client(
    saving: bool = False,
    executing: bool = False,
) -> AsyncMock:
    """Build an AsyncMock client with preset replay_status."""
    mock = AsyncMock()
    mock.replay_status.return_value = {
        "saving": saving,
        "executing": executing,
    }
    mock.read_u32.return_value = 0x1234
    mock.read_u16.return_value = 0x5678
    mock.read_u8.return_value = 0x42
    return mock


# ============================================================================
# Step validation
# ============================================================================


class TestStepValidation:
    """batch_step validates step structure before execution."""

    @pytest.mark.asyncio
    async def test_empty_steps_raises(self, monkeypatch):
        mock = _make_mock_client()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="steps must not be empty"):
            await batch_step(session_id="s", steps=[])

    @pytest.mark.asyncio
    async def test_step_missing_type_raises(self, monkeypatch):
        mock = _make_mock_client()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="missing required 'type'"):
            await batch_step(session_id="s", steps=[{"button": "cross"}])

    @pytest.mark.asyncio
    async def test_step_invalid_type_raises(self, monkeypatch):
        mock = _make_mock_client()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="invalid type"):
            await batch_step(session_id="s", steps=[{"type": "bogus"}])

    @pytest.mark.asyncio
    async def test_press_missing_button_raises(self, monkeypatch):
        mock = _make_mock_client()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="requires 'button'"):
            await batch_step(session_id="s", steps=[{"type": "press", "duration": 30}])

    @pytest.mark.asyncio
    async def test_press_invalid_button_raises(self, monkeypatch):
        mock = _make_mock_client()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="invalid button"):
            await batch_step(
                session_id="s",
                steps=[{"type": "press", "button": "bogus_btn"}],
            )

    @pytest.mark.asyncio
    async def test_wait_missing_frames_raises(self, monkeypatch):
        mock = _make_mock_client()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="requires 'frames'"):
            await batch_step(session_id="s", steps=[{"type": "wait"}])

    @pytest.mark.asyncio
    async def test_negative_duration_raises(self, monkeypatch):
        mock = _make_mock_client()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="duration must be int >= 0"):
            await batch_step(
                session_id="s",
                steps=[{"type": "press", "button": "cross", "duration": -1}],
            )

    @pytest.mark.asyncio
    async def test_negative_frames_raises(self, monkeypatch):
        mock = _make_mock_client()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="frames must be int >= 0"):
            await batch_step(session_id="s", steps=[{"type": "wait", "frames": -5}])


# ============================================================================
# Recording-mode screenshot auto-skip (spike U2 invariant)
# ============================================================================


class TestRecordingModeScreenshotSkip:
    """screenshot steps are auto-skipped when recording_mode=True."""

    @pytest.mark.asyncio
    async def test_screenshot_skipped_when_recording(self, monkeypatch):
        mock = _make_mock_client(saving=True)
        _patch_client(monkeypatch, mock)
        result = await batch_step(
            session_id="s",
            steps=[
                {"type": "press", "button": "cross", "duration": 1},
                {"type": "screenshot"},
            ],
        )
        assert result["recording_mode"] is True
        assert result["skipped"] == 1
        assert result["succeeded"] == 1
        assert result["failed"] == 0
        # The screenshot step result must be 'skipped' with reason.
        screenshot_step = result["results"][1]
        assert screenshot_step["status"] == "skipped"
        assert "recording" in screenshot_step["error"]

    @pytest.mark.asyncio
    async def test_screenshot_executed_when_not_recording(self, monkeypatch):
        mock = _make_mock_client(saving=False)
        _patch_client(monkeypatch, mock)

        # Patch the screenshot tool to avoid real framebuffer capture.
        async def fake_screenshot(**kwargs):
            return ['{"mode":"render","size_bytes":100,"width":480,"height":272}']

        monkeypatch.setattr("ppsspp_dfx_mcp.tools.screenshot.screenshot", fake_screenshot)
        result = await batch_step(
            session_id="s",
            steps=[{"type": "screenshot"}],
        )
        assert result["recording_mode"] is False
        assert result["skipped"] == 0
        assert result["succeeded"] == 1


# ============================================================================
# Step routing — press / wait / state_probe
# ============================================================================


class TestStepRouting:
    """batch_step routes each step type to the right primitive."""

    @pytest.mark.asyncio
    async def test_press_calls_client_press_button(self, monkeypatch):
        mock = _make_mock_client()
        _patch_client(monkeypatch, mock)
        result = await batch_step(
            session_id="s",
            steps=[{"type": "press", "button": "cross", "duration": 30}],
        )
        mock.press_button.assert_awaited_once_with(button="cross", duration=30)
        assert result["succeeded"] == 1
        step = result["results"][0]
        assert step["data"] == {"button": "cross", "duration": 30}

    @pytest.mark.asyncio
    async def test_wait_sleeps_without_client_call(self, monkeypatch):
        mock = _make_mock_client()
        _patch_client(monkeypatch, mock)
        # W3: the shared waiter validates session liveness mid-wait —
        # patch it out (the mock session_client bypasses the real one).
        monkeypatch.setattr(
            "ppsspp_dfx_mcp.session.client_helper.validate_session_alive",
            _async_noop,
        )
        result = await batch_step(
            session_id="s",
            steps=[{"type": "wait", "frames": 2}],  # 2 frames ~33ms
        )
        # wait step must NOT call any client method except replay_status.
        mock.press_button.assert_not_awaited()
        mock.read_u32.assert_not_awaited()
        assert result["succeeded"] == 1
        step = result["results"][0]
        assert step["data"]["frames"] == 2

    @pytest.mark.asyncio
    async def test_state_probe_delegates_to_observer(self, monkeypatch):
        _reset_registry()
        mock = _make_mock_client()
        mock.read_u32.return_value = 0x1234
        _patch_client(monkeypatch, mock)
        # Pre-register a probe so state_probe step has something to observe.
        from ppsspp_dfx_mcp.tools.state_observer import state_observer

        await state_observer(
            session_id="s",
            action="register",
            name="probe1",
            address=0x1000,
        )
        result = await batch_step(
            session_id="s",
            steps=[
                {
                    "type": "state_probe",
                    "names": "probe1",
                    "samples": 1,
                }
            ],
        )
        assert result["succeeded"] == 1
        step = result["results"][0]
        # state_probe step data is the observe result dict.
        assert step["data"]["action"] == "observe"
        assert step["data"]["observations"][0]["value"] == "0x00001234"


# ============================================================================
# on_failure semantics
# ============================================================================


class TestOnFailure:
    """on_failure='abort' stops batch on first failure."""

    @pytest.mark.asyncio
    async def test_abort_stops_after_failure(self, monkeypatch):
        mock = _make_mock_client()
        # First press_button raises, second should never be called.
        mock.press_button.side_effect = [
            RuntimeError("simulated failure"),
            None,
        ]
        _patch_client(monkeypatch, mock)
        # F-7 fix (2026-09-06): a failed batch raises ToolError carrying
        # the full result envelope — no more hollow ok responses.
        with pytest.raises(ToolError) as exc_info:
            await batch_step(
                session_id="s",
                steps=[
                    {"type": "press", "button": "cross", "duration": 1},
                    {"type": "press", "button": "circle", "duration": 1},
                ],
                on_failure="abort",
            )
        assert exc_info.value.code == "BATCH_STEP_FAILED"
        msg = str(exc_info.value)
        assert "aborted" in msg and "1 failed" in msg
        # S6 fix: compact failure summary replaces the full JSON envelope.
        assert "step[0] press: simulated failure" in msg
        assert "details=" not in msg

    @pytest.mark.asyncio
    async def test_continue_runs_all_steps_after_failure(self, monkeypatch):
        mock = _make_mock_client()
        mock.press_button.side_effect = [
            RuntimeError("simulated failure"),
            None,
        ]
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError) as exc_info:
            await batch_step(
                session_id="s",
                steps=[
                    {"type": "press", "button": "cross", "duration": 1},
                    {"type": "press", "button": "circle", "duration": 1},
                ],
                on_failure="continue",
            )
        assert exc_info.value.code == "BATCH_STEP_FAILED"
        msg = str(exc_info.value)
        # continue-mode: both steps ran, one failed, none aborted.
        assert '"aborted": false' not in msg  # S6: no JSON envelope
        assert "1 failed" in msg and "1 succeeded" in msg
        assert "step[0] press: simulated failure" in msg
        assert "details=" not in msg


# ============================================================================
# Aggregate counts
# ============================================================================


class TestAggregateCounts:
    """BatchResult counts are consistent."""

    @pytest.mark.asyncio
    async def test_counts_sum_to_total(self, monkeypatch):
        mock = _make_mock_client(saving=True)  # trigger screenshot skip
        _patch_client(monkeypatch, mock)
        monkeypatch.setattr(
            "ppsspp_dfx_mcp.session.client_helper.validate_session_alive",
            _async_noop,
        )
        result = await batch_step(
            session_id="s",
            steps=[
                {"type": "press", "button": "cross", "duration": 1},
                {"type": "wait", "frames": 1},
                {"type": "screenshot"},  # skipped (recording)
            ],
        )
        assert result["total"] == 3
        # executed = total - skipped.
        assert result["executed"] == result["total"] - result["skipped"]
        # succeeded + failed = executed.
        assert result["succeeded"] + result["failed"] == result["executed"]
