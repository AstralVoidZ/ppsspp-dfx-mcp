"""L4 regression tests for G-11 + G-12 (T060): resume budget + position.

Deep-test findings (mcp_test_report/tools/ppsspp_step.md):
- G-11: a successful ``step(action='resume')`` took ~3.01s — the full
  3000ms ``wait_for_resume`` budget — because PPSSPP emits the
  ``cpu.resume`` broadcast only on a real stepping→running transition
  (SteppingBroadcaster.cpp:64). Called while the CPU already runs, the
  budget was burned in full before the polling fallback (which then
  succeeded instantly).
- G-12: ``step(action='resume')`` returned ``pc=0x0 / ticks=0.0``,
  forcing callers into a follow-up query to learn where the CPU resumed.

Real-machine measurements backing the fix (2026-10-05, N=12
pause→resume cycles): transition broadcast confirmed in p50=5ms /
p95=6ms / max=7ms → ``_RESUME_BROADCAST_WAIT_MS = 300`` (~50x margin).

Test groups:
- G-11: the budget constant is a measured-short bounded value (a revert
  to the old 3000ms full-burn budget turns this red).
- G-12: resume reads a NON-pausing ``cpu.status`` snapshot for pc/ticks,
  strictly AFTER the resume command; a snapshot failure degrades to
  0/0.0 without failing the already-successful resume.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.core.stepping import _RESUME_BROADCAST_WAIT_MS
from ppsspp_dfx_mcp.tools.step import step


class TestG11ResumeBroadcastBudget:
    """G-11: the broadcast fast path must be bounded by a measured-short budget."""

    def test_budget_is_measured_short_not_the_old_full_burn(self) -> None:
        """The budget must be a short cycle, not the old 3000ms full burn.

        Falsifiability: restoring ``timeout_ms=3000`` (or any value > 500)
        makes this fail. The bound encodes the measured p95=6ms with a
        generous headroom while keeping the no-broadcast degenerate case
        (already-running resume) well under a second.
        """
        assert 0 < _RESUME_BROADCAST_WAIT_MS <= 500, (
            f"_RESUME_BROADCAST_WAIT_MS={_RESUME_BROADCAST_WAIT_MS} — the "
            "resume broadcast fast path must stay a measured-short cycle "
            "(measured p95=6ms on 2026-10-05). A 3000ms budget burns the "
            "full wait on every already-running resume (G-11: ~3.01s "
            "successful resumes)."
        )


def _patch_session_client(monkeypatch: pytest.MonkeyPatch, client: AsyncMock) -> None:
    @asynccontextmanager
    async def fake_session_client(session_id: str) -> AsyncIterator[AsyncMock]:
        yield client

    monkeypatch.setattr(
        "ppsspp_dfx_mcp.tools.step.session_client",
        fake_session_client,
    )


class TestG12ResumePositionSnapshot:
    """G-12: resume returns a LOW-trust pc/ticks snapshot, best-effort."""

    @pytest.mark.asyncio
    async def test_cpu_status_is_read_after_the_resume_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Order invariant: resume() precedes the cpu.status snapshot.

        Reading the status BEFORE the resume would report the pre-resume
        (stepping) state — the snapshot is only meaningful after the CPU
        is running again.
        """
        client = AsyncMock()
        call_order: list[str] = []

        async def track_resume() -> dict[str, Any]:
            call_order.append("resume")
            return {"stepping": False}

        async def track_cpu_status() -> dict[str, Any]:
            call_order.append("cpu_status")
            return {"stepping": False, "pc": 0x08808500, "ticks": 42}

        client.resume.side_effect = track_resume
        client.cpu_status.side_effect = track_cpu_status
        _patch_session_client(monkeypatch, client)

        result = await step(session_id="sess-1", action="resume")

        assert call_order == ["resume", "cpu_status"], (
            "step(resume) must issue cpu.resume BEFORE reading cpu.status — "
            f"got {call_order}. If reversed, pc reflects the paused state."
        )
        assert result["pc"] == "0x08808500"
        assert result["ticks"] == 42.0

    @pytest.mark.asyncio
    async def test_snapshot_failure_degrades_to_zero_without_failing_resume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cpu.status failure must not fail the already-successful resume.

        The snapshot is supplemental (best-effort): the CPU IS running, so
        raising here would report a failed resume that actually succeeded.
        Falsifiability: removing the tool-layer try/except makes this fail
        with the propagated RuntimeError instead of pc=0x00000000.
        """
        client = AsyncMock()
        client.resume.return_value = {"stepping": False}
        client.cpu_status.side_effect = RuntimeError("transport closed mid-read")
        _patch_session_client(monkeypatch, client)

        result = await step(session_id="sess-1", action="resume")

        assert result["action"] == "resume"
        assert result["pc"] == "0x00000000", (
            "On snapshot failure the resume result must degrade to pc=0 "
            "(documented fallback) — not raise and not invent a location."
        )
        assert result["ticks"] == 0.0
        client.resume.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_missing_pc_field_degrades_to_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cpu.status dict without pc/ticks (protocol drift) degrades to 0.

        Guards the .get() default path: a truncated response must not
        fabricate a location or crash the tool.
        """
        client = AsyncMock()
        client.resume.return_value = {"stepping": False}
        client.cpu_status.return_value = {"stepping": False}
        _patch_session_client(monkeypatch, client)

        result = await step(session_id="sess-1", action="resume")

        assert result["pc"] == "0x00000000"
        assert result["ticks"] == 0.0
