"""Step-confirmation helpers — extracted from PpssppDebugClient.

The stale-broadcast filter builder and the legacy ``cpu.status`` poll
are pure transport-level helpers with no client state of their own.
They live here so the confirmation logic can be reasoned about (and
unit-tested) independently; ``PpssppDebugClient`` keeps thin delegating
methods with identical signatures.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ppsspp_dfx_mcp.core.transport import WsTransport


def build_step_filter(
    pre_pc: int | None,
    pre_ticks: float | None,
) -> Callable[[dict[str, Any]], bool] | None:
    """Build a stale-broadcast filter for step confirmation.

    Extracted implementation of ``PpssppDebugClient._build_step_filter``
    — see that method for the full contract docstring.
    """
    if pre_pc is None and pre_ticks is None:
        return None

    def _filter(msg: dict[str, Any]) -> bool:
        pc = msg.get("pc")
        ticks = msg.get("ticks")
        # Each known field independently indicates change; unknown
        # fields are excluded from the OR (treated as "no change"),
        # so a stale broadcast whose only-known field matches the
        # pre-step value is rejected.
        pc_changed = pre_pc is not None and pc != pre_pc
        ticks_changed = pre_ticks is not None and ticks != pre_ticks
        return pc_changed or ticks_changed

    return _filter


async def confirm_step_completed_legacy(
    transport: WsTransport,
    timeout_ms: int = 5000,
    interval_ms: int = 200,
) -> dict[str, Any]:
    """Legacy step confirmation: poll cpu.status until stepping=True.

    Extracted implementation of
    ``PpssppDebugClient._confirm_step_completed_legacy`` — see that
    method for the full contract docstring.
    """
    return await transport.wait_for_state(
        lambda s: s.get("stepping") is True,
        timeout_ms=timeout_ms,
        interval_ms=interval_ms,
    )
