"""L3 orchestration tests: step(pause) populates PC via get_pc.

Batch 1 (b1-4): the `step(action="pause")` tool now orchestrates
pause → get_pc → StepResult(pc=...). Previously pause returned
StepResult(pc=0), forcing callers to issue a separate get_pc
round-trip to learn the pause location.

L3 focus (NOT covered by L1 / L4):
- Tool-layer orchestration: step(pause) calls client.pause() THEN
  client.get_pc(), populating StepResult.pc.
- Order invariant: pause precedes get_pc (get_pc on a running CPU
  returns LOW-trust PC — see cpu.getAllRegs WS contract).
- resume / reset do NOT populate pc (no trustworthy PC available).

The test mocks `session_client` (following the test_v016 pattern)
to inject a mock DebugClient, verifying the tool-layer glue without
a real WebSocket connection.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.errors import ToolError
from ppsspp_dfx_mcp.tools.step import step


class TestStepPausePopulatesPc:
    """L3: step(action="pause") orchestrates pause → get_pc → StepResult.

    Batch 1 (b1-4): after client.pause() puts the CPU in stepping
    mode, the tool calls client.get_pc() to extract the trustworthy
    PC (cpu.getAllRegs trust=LOW caveat only applies when CPU is
    running — see CPUCoreSubscriber.cpp:105). The PC is populated
    into StepResult.pc so callers can branch on the pause location
    without a separate get_pc round-trip.
    """

    @pytest.mark.asyncio
    async def test_pause_populates_pc_from_get_pc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """step(pause) returns StepResult with pc from get_pc.

        Verifies the tool-layer orchestration: pause() is called,
        then get_pc() is called, and the returned PC is populated
        into the StepResult.
        """
        mock_client = AsyncMock()
        mock_client.pause.return_value = {"stepping": True}
        mock_client.safe_get_pc.return_value = (0x08808400, "high")

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.step.session_client",
            fake_session_client,
        )

        result = await step(session_id="sess-1", action="pause")

        assert result["action"] == "pause"
        assert result["pc"] == "0x08808400", (
            "step(pause) must populate pc from safe_get_pc() — b1-4 fix. "
            "If pc is 0, the pause→safe_get_pc orchestration was removed."
        )

    @pytest.mark.asyncio
    async def test_pause_calls_get_pc_after_pause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Order invariant: pause() precedes get_pc().

        get_pc() on a running CPU returns a LOW-trust PC (WS contract
        for cpu.getAllRegs: trust=LOW, caveat "PC inaccurate unless
        CPU is stepping"). The tool must call pause() first to enter
        stepping mode, THEN call get_pc().
        """
        mock_client = AsyncMock()
        mock_client.pause.return_value = {"stepping": True}
        mock_client.safe_get_pc.return_value = (0x08804000, "high")

        call_order: list[str] = []

        async def track_pause() -> dict[str, Any]:
            call_order.append("pause")
            return {"stepping": True}

        async def track_safe_get_pc() -> tuple[int, str]:
            call_order.append("safe_get_pc")
            return (0x08804000, "high")

        mock_client.pause.side_effect = track_pause
        mock_client.safe_get_pc.side_effect = track_safe_get_pc

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.step.session_client",
            fake_session_client,
        )

        await step(session_id="sess-1", action="pause")

        assert call_order == ["pause", "safe_get_pc"], (
            "step(pause) must call pause() BEFORE safe_get_pc() — "
            "safe_get_pc on a running CPU returns LOW-trust PC. "
            "If order differs, the orchestration is broken."
        )

    @pytest.mark.asyncio
    async def test_pause_get_pc_failure_surfaces_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If safe_get_pc raises after pause, the error surfaces (not swallowed)."""
        mock_client = AsyncMock()
        mock_client.pause.return_value = {"stepping": True}
        mock_client.safe_get_pc.side_effect = RuntimeError("transport closed")

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.step.session_client",
            fake_session_client,
        )

        # The tool wraps non-ToolError exceptions via to_tool_error().
        # The error must surface (not be swallowed into pc=0).
        with pytest.raises((RuntimeError, ToolError), match="transport closed"):
            await step(session_id="sess-1", action="pause")


class TestStepResumeResetDoNotPopulatePc:
    """L3: step(resume) and step(reset) do NOT populate pc.

    resume: CPU transitions to running, PC no longer trustworthy.
    reset: game reboots, PC irrelevant.
    Both return StepResult(pc=0).
    """

    @pytest.mark.asyncio
    async def test_resume_does_not_call_get_pc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """step(resume) must NOT call get_pc (CPU transitioning to running)."""
        mock_client = AsyncMock()
        mock_client.resume.return_value = {"stepping": False}

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.step.session_client",
            fake_session_client,
        )

        result = await step(session_id="sess-1", action="resume")

        assert result["action"] == "resume"
        assert result["pc"] == "0x00000000", (
            "step(resume) must NOT populate pc — CPU is transitioning "
            "to running, PC is not trustworthy."
        )
        mock_client.get_pc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reset_does_not_call_get_pc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """step(reset) must NOT call get_pc (game reboots, PC irrelevant)."""
        mock_client = AsyncMock()
        mock_client.reset.return_value = {"ok": True}

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.step.session_client",
            fake_session_client,
        )

        result = await step(session_id="sess-1", action="reset")

        assert result["action"] == "reset"
        assert result["pc"] == "0x00000000", (
            "step(resume) must NOT populate pc — game reboots, PC is irrelevant."
        )
        mock_client.get_pc.assert_not_awaited()
