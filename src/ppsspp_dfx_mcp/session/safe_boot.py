"""Boot-readiness probe + wedge quarantine.

Single implementation of the CPU-start probe shared by:
- ``ppsspp_session(action='wait_ready')`` (tools/session.py)
- ``SessionManager.start_session(resilient=True)`` (wedge self-heal)

The probe signal is the one validated on real boots by the test harness
and integration conftest: ``memory.read_u32`` at the top.prx load base
starts succeeding only once the emulated CPU is up (PPSSPP answers
WebSocket BEFORE the CPU starts — ``game_status.paused=False`` is a
false positive at that stage).

Also hosts the wedge-quarantine helper: the documented boot-wedge root
cause is a persisted GPU-backend failure blacklist
(``memstick/PSP/SYSTEM/FailedGraphicsBackends.txt`` accumulating
``DIRECT3D11,VULKAN`` after repeated force-kills — see
docs/archive/analysis/analysis_ppsspp_dfx_mcp_review_r2_fixes_v1.md
§3.5). Quarantine RENAMES the file (never deletes) so the evidence chain
survives; PPSSPP regenerates the file from scratch on next boot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from ppsspp_dfx_mcp.errors import BootTimeout

logger = logging.getLogger(__name__)

# Default readiness probe address: the project's top.prx load base
# (addresses.yaml top_base) — the same probe the harness validated.
DEFAULT_PROBE_ADDR = 0x08804000

# How long a wedged PPSSPP typically needs between kill and next launch
# to release the GPU device / port before the relaunch.
_WEDGE_COOLDOWN_S = 2.0


async def probe_cpu_ready(
    transport: Any,
    *,
    probe_addr: int = DEFAULT_PROBE_ADDR,
    budget_s: float = 75.0,
    poll_interval_s: float = 1.0,
    alive_check: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Poll a probe read until the emulated CPU is up.

    PPSSPP answers WebSocket requests before the emulated CPU starts, so
    this polls ``memory.read_u32(probe_addr)`` — the exact readiness
    signal the test harness and integration conftest validated on real
    boots — until it succeeds.

    Args:
        transport: A connected WsTransport (or stub) used for the probe
            reads. One raw ticketed call per poll — NO session lock is
            taken here (callers decide lock policy; both current callers
            are lock-free by contract).
        probe_addr: Address polled (default top.prx base 0x08804000).
        budget_s: Total wait budget before declaring a wedge.
        poll_interval_s: Cadence between probe reads.
        alive_check: Optional liveness probe — when provided and it
            returns False, the boot is declared dead immediately instead
            of polling out the full budget (the process exiting mid-boot
            is decisive wedge evidence).

    Returns:
        ``{"ready": True, "elapsed_s": <float>, "probe_value": <int>}``.

    Raises:
        BootTimeout: probe never succeeded within budget_s (message
            carries wedge-recovery guidance for agents).
        Exception: probe failures that mean more than "not ready yet"
            propagate only when ``alive_check`` is None — with an
            alive_check configured, transient transport errors are
            absorbed until the budget or the liveness check decides.
    """
    started = time.monotonic()
    deadline = started + budget_s
    last_error = ""
    while True:
        if alive_check is not None:
            try:
                if not alive_check():
                    raise BootTimeout(
                        "emulated CPU did not start: the PPSSPP process "
                        f"died during boot (probe at 0x{probe_addr:08X} "
                        f"after {time.monotonic() - started:.1f}s; last "
                        f"error: {last_error or 'none'}). The session is "
                        "wedged, not merely slow: check "
                        "ppsspp_analyze_log for boot errors (GPU backend "
                        "failure records are a known cause), then stop "
                        "and restart the session."
                    )
            except BootTimeout:
                raise
            except Exception:  # noqa: BLE001 — liveness probe is best-effort
                pass
        try:
            resp = await transport.call("memory.read_u32", address=probe_addr)
            return {
                "ready": True,
                "elapsed_s": round(time.monotonic() - started, 3),
                "probe_value": int(resp.get("value", 0)),
            }
        except Exception as e:  # noqa: BLE001 — any probe failure
            # (PPSSPP protocol "CPU not started", transient transport
            # errors) means "not ready yet" — keep polling.
            # asyncio.CancelledError is a BaseException: propagates.
            last_error = str(e)[:300]
        if time.monotonic() >= deadline:
            raise BootTimeout(
                f"emulated CPU did not start within {budget_s:.0f}s "
                f"(probe read at 0x{probe_addr:08X} kept failing; last "
                f"error: {last_error or 'none'}). The session is likely "
                f"wedged, not merely slow: check ppsspp_analyze_log for "
                f"boot errors (GPU backend failure records are a known "
                f"cause), then stop and restart the session."
            )
        await asyncio.sleep(poll_interval_s)


def quarantine_gpu_backend_blacklist(exe_path: Path) -> Path | None:
    """Quarantine the GPU-backend failure blacklist (rename, never delete).

    The wedge family root cause: repeated force-kills of a GPU-holding
    PPSSPP persist backend init failures into
    ``<exe_dir>/memstick/PSP/SYSTEM/FailedGraphicsBackends.txt``; later
    boots then find no usable graphics backend and the CPU never starts.
    Renaming the file clears the blacklist for the next boot while
    keeping the evidence on disk (``*.bak-<timestamp>``).

    Returns:
        The quarantine target path, or None when there was nothing to
        quarantine (file absent) or the rename failed (logged, non-fatal
        — boot proceeds without the heal and the wedge will resurface).
    """
    blacklist = (
        Path(exe_path).resolve().parent
        / "memstick"
        / "PSP"
        / "SYSTEM"
        / "FailedGraphicsBackends.txt"
    )
    if not blacklist.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = blacklist.with_name(f"{blacklist.name}.bak-{stamp}")
    try:
        blacklist.rename(target)
        logger.warning(
            "wedge heal: quarantined GPU backend blacklist %s -> %s "
            "(root cause per review_r2_fixes §3.5: persisted backend "
            "failure records from force-killed GPU-holding processes)",
            blacklist,
            target,
        )
        return target
    except OSError as e:
        logger.warning(
            "wedge heal: failed to quarantine %s (non-fatal, boot continues without the heal): %s",
            blacklist,
            e,
        )
        return None


def wedge_cooldown() -> None:
    """Sleep between self-heal restart attempts (releases GPU/port)."""
    time.sleep(_WEDGE_COOLDOWN_S)
