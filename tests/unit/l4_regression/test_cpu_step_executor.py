"""ISS-001 regression: batch cpu_step must really step.

The executor branch was missing entirely (D7): cpu_step passed
validation, sent no WS call, and reported success with an unmoved PC.
These tests lock in the executor wiring: mode→primitive mapping, count
loop, per-step timeout surfacing as a step failure with partial
progress, and step_data accounting.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.tools import batch_step as bs
from ppsspp_dfx_mcp.tools.batch_step import batch_step


def _mock_client(mode: str, pcs: list[str]) -> AsyncMock:
    from contextlib import asynccontextmanager

    client = AsyncMock()
    # W8 (review v4): `replay_status` must resolve to a DICT. An unconfigured
    # AsyncMock resolves to another AsyncMock, whose `.get()` returns a
    # coroutine — `bool(<coroutine>)` is True, so `_execute_batch` silently
    # reported `recording_mode=True` (screenshots auto-skipped for the wrong
    # reason) and leaked an un-awaited coroutine per call.
    client.replay_status.return_value = {"executing": False, "saving": False}
    stepper = AsyncMock(side_effect=[{"pc": pc, "ticks": 1000 + i} for i, pc in enumerate(pcs)])
    setattr(client, f"step_{mode}", stepper)

    # The executor wraps stepping in with_stepping — provide a no-op CM.
    @asynccontextmanager
    async def fake_ws():
        yield

    client.with_stepping = lambda: fake_ws()
    return client


def _patch(monkeypatch: pytest.MonkeyPatch, client: AsyncMock) -> None:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_session_client(session_id: str):
        yield client

    monkeypatch.setattr(bs, "session_client", fake_session_client)


@pytest.mark.asyncio
async def test_cpu_step_into_loops_count_and_reports(monkeypatch):
    client = _mock_client("into", ["0x100", "0x104", "0x108"])
    _patch(monkeypatch, client)
    resp = await batch_step(
        session_id="s1",
        steps=[{"type": "cpu_step", "mode": "into", "count": 3}],
    )
    assert resp["succeeded"] == 1
    # W8 seam: `recording_mode` is derived from the replay status reply, so a
    # double that returns a coroutine (instead of a dict) flips it to True.
    assert resp["recording_mode"] is False
    client.step_into.assert_awaited()
    assert client.step_into.await_count == 3
    data = resp["results"][0]["data"]
    assert data == {
        "mode": "into",
        "requested": 3,
        "stepped": 3,
        "last_pc": "0x108",
        "last_ticks": 1002,
        "resumed_after": True,
    }


@pytest.mark.asyncio
async def test_cpu_step_mode_mapping(monkeypatch):
    for mode in ("over", "out"):
        client = _mock_client(mode, ["0x200"])
        _patch(monkeypatch, client)
        await batch_step(session_id="s1", steps=[{"type": "cpu_step", "mode": mode}])
        getattr(client, f"step_{mode}").assert_awaited_once()


@pytest.mark.asyncio
async def test_cpu_step_stall_reports_partial_progress(monkeypatch):
    from ppsspp_dfx_mcp.errors import ToolError

    client = _mock_client("into", ["0x100"])
    client.step_into.side_effect = [
        {"pc": "0x100", "ticks": 1},
        TimeoutError("step not confirmed"),
    ]
    _patch(monkeypatch, client)
    # Contract: any failed step makes the whole call raise BATCH_STEP_FAILED
    # (on_failure only controls whether LATER steps still run).
    with pytest.raises(ToolError) as ei:
        await batch_step(
            session_id="s1",
            steps=[{"type": "cpu_step", "mode": "into", "count": 5}],
            on_failure="continue",
        )
    msg = str(ei.value)
    assert "cpu_step" in msg
    assert "1/5" in msg  # partial progress surfaced
    assert "stalled" in msg


@pytest.mark.asyncio
async def test_cpu_step_invalid_mode_rejected_at_validation():
    with pytest.raises(bs.StepInvalid):
        await batch_step(
            session_id="s1",
            steps=[{"type": "cpu_step", "mode": "sideways"}],
        )


@pytest.mark.asyncio
async def test_cpu_step_timeout_derives_from_batch_deadline(monkeypatch):
    """Review-v4 W-4: cpu_step per-step timeout must shrink to the batch
    deadline exactly like the state_probe branch does.

    The foreground gate admits a batch by estimate (~0.05s/step); a fixed
    10s per-step timeout let a single stalled step burn 10s while holding
    the session lock — 45 steps = 450s, the exact freeze the budget gate
    exists to prevent. With a 2s batch budget, the observed per-step
    timeout must be ~1-2s (the helper's 1s floor), never 10000ms.
    """
    client = _mock_client("into", [])
    seen: list[int] = []

    async def fake_step(**kwargs):
        seen.append(kwargs.get("timeout_ms"))
        return {"pc": "0x100", "ticks": 1}

    client.step_into.side_effect = fake_step
    _patch(monkeypatch, client)
    await bs._execute_batch(
        session_id="s1",
        steps=[{"type": "cpu_step", "mode": "into", "count": 30}],
        on_failure="continue",
        budget_s=2.0,
    )
    assert seen, "cpu_step must reach the stepper"
    assert all(t is not None and t <= 2000 for t in seen), seen[:5]
