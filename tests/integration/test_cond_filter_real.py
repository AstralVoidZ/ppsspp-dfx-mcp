"""Real-PPSSPP lock for the MCP-side condition evaluator (🔴-1/D1).

PPSSPP v1.20.4 (IR mode) silently ignores register-referencing break
conditions: a breakpoint armed with ``s1==0x711`` still fires when
``s1`` is anything else. The fix keeps the condition MCP-side and only
surfaces hits whose expression actually holds.

Deterministic real-hardware proof (ordering matters — a breakpoint hit
that lands BEFORE ``wait`` starts is reported as ``already_paused`` and
is not attributed to the evaluator):

1. find a hot PC: sample the running PC a few times and require it to
   repeat (the game's main loop revisits addresses constantly);
2. start ``wait`` FIRST while the CPU is running (its entry probe must
   not see a paused CPU);
3. arm ``s1==0x711`` at the hot PC — with ``wait`` already in its hit
   loop, any hit lands inside the window and the evaluator acts on it;
4. assert the ``wait`` never reports a hit **and** that at least one
   falsy hit was actually filtered (``filtered_hits >= 1``).

Step 4's second half is the evidence: a bare ``hit=False`` could also
mean "the breakpoint never fired" — the first draft of this test passed
for exactly that wrong reason during review.

Env-gated: requires ``PPSSPP_DFX_TEST_EXE_PATH`` +
``PPSSPP_DFX_TEST_ISO_PATH`` (skips otherwise, like the rest of
tests/integration/).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections import Counter

import pytest

if not os.environ.get("PPSSPP_DFX_TEST_EXE_PATH") or not os.environ.get("PPSSPP_DFX_TEST_ISO_PATH"):
    # 与 tests/integration/conftest.py 的 real_exe / real_iso 门控同源：保留
    # "PPSSPP executable not configured" / "Game ISO not configured" 前缀，
    # 以便 scripts/check_skips.py 的既有白名单直接覆盖，无需放宽审计。
    pytest.skip(
        "PPSSPP executable not configured (set PPSSPP_DFX_TEST_EXE_PATH to "
        "your PPSSPP binary) and/or Game ISO not configured (set "
        "PPSSPP_DFX_TEST_ISO_PATH to your ISO path) "
        "— real-device gate: CI never sets these envs, so this test does not run in the CI gate",
        allow_module_level=True,
    )

pytestmark = [pytest.mark.real_ppsspp, pytest.mark.integration]


async def _await_cpu_executing(client, budget_s: float = 20.0, gap_s: float = 0.25) -> None:
    """Block until the emulated CPU actually advances, else skip.

    Why this exists (measured 2026-09-30, flaky under full-suite load):
    PPSSPP answers WebSocket requests *before* the emulated CPU starts, so
    a freshly launched session is reachable while still showing ``ticks=0``.
    ``_sample_running_pc`` then calls ``client.pause()``, whose
    ``wait_for_state(stepping=True)`` times out after 3s and raises
    ``SteppingFailedError`` -- a startup race, not a condition-filter
    regression.

    Repeating the pause attempt is the readiness probe: once the CPU is
    running, pause succeeds immediately. A bounded budget keeps this from
    masking a genuine wedge (after the budget we skip, naming the reason,
    rather than asserting on a CPU that never ran).

    Skips (does not fail) because "the game did not start within 20s" is an
    environment condition, not a defect in the code under test.
    """
    from ppsspp_dfx_mcp.errors import SteppingFailedError

    deadline = time.monotonic() + budget_s
    attempts = 0
    while time.monotonic() < deadline:
        attempts += 1
        try:
            await client.pause()
        except (SteppingFailedError, TimeoutError, Exception) as exc:  # noqa: B014
            last = type(exc).__name__
        else:
            await client.resume()
            return
        await asyncio.sleep(gap_s)

    pytest.skip(
        f"emulated CPU did not start within {budget_s:.0f}s after "
        f"{attempts} pause attempts (last error: {last}); the session is "
        f"reachable but not executing -- startup race, not a "
        f"condition-filter regression"
    )


async def _sample_running_pc(client, n: int = 4, gap_s: float = 0.15) -> int:
    """Sample the PC of a RUNNING cpu; returns the most frequent address."""
    pcs: list[int] = []
    for _ in range(n):
        await client.pause()
        pc, _trust = await client.safe_get_pc()
        await client.resume()
        pcs.append(pc)
        await asyncio.sleep(gap_s)
    return Counter(pcs).most_common(1)[0][0]


async def test_conditional_breakpoint_does_not_hit_on_falsy_register(real_session):
    """``s1==0x711`` must not surface a hit while ``s1 != 0x711``."""
    from ppsspp_dfx_mcp.address import format_address
    from ppsspp_dfx_mcp.core import cond_filter
    from ppsspp_dfx_mcp.session.client_helper import session_client
    from ppsspp_dfx_mcp.tools.breakpoint import breakpoint as bp_tool
    from ppsspp_dfx_mcp.tools.evaluate import _extract_value

    sid = real_session
    async with session_client(sid) as client:
        await _await_cpu_executing(client)
        hot_pc = await _sample_running_pc(client)
        resp = await client.evaluate(expression="s1")
    s1 = _extract_value(resp if isinstance(resp, dict) else None)
    if s1 is None:
        pytest.skip("cpu.evaluate('s1') unavailable on this build")
    if s1 == 0x711:
        pytest.skip("s1 sampled as 0x711 — cannot exercise the falsy branch")

    addr = format_address(hot_pc)
    # 先起 wait（此时 CPU 在跑，入口探测不会命中 already_paused 短路），
    # 再布防——命中原子上落在 wait 的窗口内，求值器才有机会介入。
    wait_task = asyncio.create_task(bp_tool(session_id=sid, action="wait", timeout_s=8.0))
    await asyncio.sleep(0.3)
    await bp_tool(session_id=sid, action="set", address=addr, condition="s1==0x711")
    try:
        assert cond_filter.get(sid, hot_pc) is not None, "condition was not registered MCP-side"
        out = await wait_task
        assert out["hit"] is False, f"spurious conditional hit: {out}"
        assert out["filtered_hits"] >= 1, (
            f"expected >=1 filtered falsy hit at {addr} — hit=False alone would "
            f"not prove the evaluator ran: {out}"
        )
    finally:
        with contextlib.suppress(Exception):
            await bp_tool(session_id=sid, action="remove", address=addr)
        cond_filter.drop(sid, hot_pc)
