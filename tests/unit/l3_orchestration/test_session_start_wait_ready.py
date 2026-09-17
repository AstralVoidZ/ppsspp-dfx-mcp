"""L3 orchestration tests: G5 one-call boot — session start(wait_ready=True).

Anchor: research_ppsspp_dfx_best_practice_gap_audit_v1 §G5 — boot used to
require a mandatory two-call flow (start → wait_ready); `wait_ready=true`
on the start action inlines the same probe/budget semantics so the
single-session happy path costs one call. Default false keeps the
historical flow byte-for-byte.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.errors import BootTimeout
from ppsspp_dfx_mcp.models.session import Session
from ppsspp_dfx_mcp.session import session_manager
from ppsspp_dfx_mcp.tools.session import session as session_tool

pytestmark = pytest.mark.asyncio


def _fake_session() -> Session:
    return Session(session_id="sess-g5", iso_path="game.iso")


def _patch_start(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock_start = AsyncMock(return_value=_fake_session())
    monkeypatch.setattr(session_manager, "start_session", mock_start)
    return mock_start


def _patch_probe(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock_probe = AsyncMock()
    monkeypatch.setattr("ppsspp_dfx_mcp.tools.session._wait_ready_cpu", mock_probe)
    return mock_probe


class TestStartWaitReady:
    async def test_wait_ready_true_invokes_probe_in_real_mode(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """start(wait_ready=True) runs the readiness probe after boot."""
        monkeypatch.setattr("ppsspp_dfx_mcp.tools.session.test_mode", lambda: "real")
        _patch_start(monkeypatch)
        mock_probe = _patch_probe(monkeypatch)

        result = await session_tool(
            action="start",
            iso_path="game.iso",
            wait_ready=True,
            timeout_s=90.0,
        )

        assert result["session_id"] == "sess-g5"
        mock_probe.assert_awaited_once()
        args = mock_probe.await_args.args
        assert args[0] == "sess-g5"
        assert args[1] == 90.0  # clamped timeout forwarded
        assert args[2] == 0x08804000  # default probe parsed

    async def test_wait_ready_false_skips_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default (wait_ready=False) keeps the historical two-call flow."""
        monkeypatch.setattr("ppsspp_dfx_mcp.tools.session.test_mode", lambda: "real")
        _patch_start(monkeypatch)
        mock_probe = _patch_probe(monkeypatch)

        await session_tool(action="start", iso_path="game.iso")

        mock_probe.assert_not_awaited()

    async def test_wait_ready_boot_timeout_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BOOT_TIMEOUT from the inlined probe propagates (no swallow)."""
        monkeypatch.setattr("ppsspp_dfx_mcp.tools.session.test_mode", lambda: "real")
        _patch_start(monkeypatch)
        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.session._wait_ready_cpu",
            AsyncMock(side_effect=BootTimeout("cpu not started in 90s")),
        )

        with pytest.raises(BootTimeout):
            await session_tool(action="start", iso_path="game.iso", wait_ready=True)

    async def test_fake_mode_skips_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fake test mode has no boot concept — ready immediately."""
        monkeypatch.setattr("ppsspp_dfx_mcp.tools.session.test_mode", lambda: "fake")
        _patch_start(monkeypatch)
        mock_probe = _patch_probe(monkeypatch)

        await session_tool(action="start", iso_path="game.iso", wait_ready=True)

        mock_probe.assert_not_awaited()
