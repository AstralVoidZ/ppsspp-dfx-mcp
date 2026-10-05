"""S4 regression lock: the WM_COMMAND screenshot settle wait must be bounded.

Before the fix, ``CaptureService._wm_command_screenshot`` waited for the
screenshot file to settle inside an unbounded ``while stable_samples < 2``
loop. ``stable_samples`` only ever increments on ``0 < size == last_size``,
so a file that stays zero bytes long — PPSSPP killed mid-write, disk full,
or a stale file left behind by an earlier run — made that loop spin forever
(the only exit was ``f.stat()`` raising ``OSError``). The loop runs inside
``safe_screenshot()``'s ``with_stepping()`` block, i.e. while holding the
session lock with the CPU halted in STEPPING, so the whole session became
unusable.

The fix moves the settle wait into ``_await_stable_image(path, deadline)``,
which is bounded by a ``time.monotonic()`` deadline and reads the file off
the event loop. These tests lock both properties.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from pathlib import Path

import pytest

from ppsspp_dfx_mcp.service import capture
from ppsspp_dfx_mcp.service.capture import CaptureService

# Generous upper bound for "the wait gave up" — anything above this means the
# old unbounded spin is back. The deadline itself is a fraction of a second.
_HANG_GUARD_S = 5.0


async def test_zero_byte_file_returns_empty_without_hanging(tmp_path: Path) -> None:
    """S4 core: a 0-byte screenshot file must not spin forever."""
    stuck = tmp_path / "stuck.png"
    stuck.write_bytes(b"")

    data = await asyncio.wait_for(
        capture._await_stable_image(stuck, time.monotonic() + 0.25),
        timeout=_HANG_GUARD_S,
    )

    assert data == b""


async def test_settled_file_is_read(tmp_path: Path) -> None:
    """The happy path still returns the bytes once the size stops changing."""
    ready = tmp_path / "ready.png"
    ready.write_bytes(b"x" * 200)

    data = await asyncio.wait_for(
        capture._await_stable_image(ready, time.monotonic() + _HANG_GUARD_S),
        timeout=_HANG_GUARD_S,
    )

    assert data == b"x" * 200


async def test_expired_deadline_returns_immediately(tmp_path: Path) -> None:
    """An already-passed deadline yields b"" instead of one more sample."""
    ready = tmp_path / "ready.png"
    ready.write_bytes(b"x" * 200)

    started = time.monotonic()
    data = await asyncio.wait_for(
        capture._await_stable_image(ready, started),
        timeout=_HANG_GUARD_S,
    )

    assert data == b""
    assert time.monotonic() - started < 1.0


async def test_vanished_file_returns_empty(tmp_path: Path) -> None:
    """A file that disappears mid-wait must not raise out of the helper."""
    missing = tmp_path / "never-written.png"

    data = await asyncio.wait_for(
        capture._await_stable_image(missing, time.monotonic() + 0.25),
        timeout=_HANG_GUARD_S,
    )

    assert data == b""


async def test_still_growing_file_gives_up_at_the_deadline(tmp_path: Path) -> None:
    """A file whose size never repeats twice must be abandoned at the deadline."""
    growing = tmp_path / "growing.png"
    growing.write_bytes(b"x" * 100)
    stop = asyncio.Event()

    async def _grow() -> None:
        size = 100
        while not stop.is_set():
            size += 100
            growing.write_bytes(b"x" * size)
            await asyncio.sleep(0.02)

    grower = asyncio.create_task(_grow())
    try:
        started = time.monotonic()
        data = await asyncio.wait_for(
            capture._await_stable_image(growing, started + 0.25),
            timeout=_HANG_GUARD_S,
        )
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        await grower

    assert data == b""
    assert elapsed < 3.0


def test_wm_command_path_uses_the_bounded_helper() -> None:
    """Structural lock: the inline unbounded loop must not come back."""
    source = inspect.getsource(CaptureService._wm_command_screenshot)

    assert "_await_stable_image" in source
    assert "while stable_samples < 2" not in source
    # The read must be offloaded, not a synchronous read on the event loop.
    assert "read_bytes" not in source


def test_wait_budget_is_small_and_poll_is_aligned() -> None:
    """The two constants must stay internally consistent with the scan."""
    assert 0 < capture._WM_SCREENSHOT_POLL_S <= 0.25
    assert capture._WM_SCREENSHOT_WAIT_S <= 10.0
    assert pytest.approx(100 * capture._WM_SCREENSHOT_POLL_S) == capture._WM_SCREENSHOT_WAIT_S
