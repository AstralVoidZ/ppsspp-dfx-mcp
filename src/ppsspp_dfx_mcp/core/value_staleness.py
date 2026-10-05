"""core/value_staleness.py — 探针读数的陈旧地址 suspicion 状态机。

**为什么在 core 层**：这是**纯状态**（一张按会话键控的连续零读数表）加一个判定
函数，不依赖任何工具层符号。原先它住在 `tools/_common.py`，逼得 `session/` 为了
清理侧表而反向 import tools（架构审计点名的 session→tools 倒置）。下移到 core 后
`session/` 与 `tools/` 都能正向依赖它。`tools/_common.py` 保留 re-export 门面，
历史导入点不变。
"""

from __future__ import annotations

__all__ = [
    "STALE_ADDRESS_STREAK",
    "VALUE_OK",
    "VALUE_STALE_SUSPECTED",
    "classify_probe_reading",
    "reset_probe_streaks",
]

# ── Stale-address suspicion ─────────────────────────────────────────────
# A probe whose address no longer points at live state reads ZERO forever --
# which is indistinguishable, from one read alone, from "the value really is
# zero". This was a real defect: addresses.yaml's state_probes had
# drifted and every read was 0, so `passed=false` looked like a game fact.
#
# The distinction needs history. Every probe reading is therefore recorded
# here as a per-session streak: a non-zero resets it, a zero extends it, and
# once it reaches the threshold the reading is classified as
# `stale_address_suspected` -- the third state FR-019a requires, distinct
# from both 'not_configured' (no address at all) and a genuine zero.
#
# The judgement is explicitly a SUSPICION, not a verdict: a game may sit in a
# menu with game_mode == 0 for minutes. What this changes is that the reader
# is told WHICH of the two situations they are in, instead of having to guess
# (which is exactly what produced the earlier misdiagnosis).
VALUE_STALE_SUSPECTED = "stale_address_suspected"
VALUE_OK = "ok"

#: How many consecutive zero readings before the address is flagged.
#: 3 = "seen three times in a row with no non-zero in between" -- enough
#: that a single cold-start blip does not trip it, small enough to reach
#: within one `observe(samples=3)` call.
STALE_ADDRESS_STREAK = 3

#: session_id -> {address: consecutive zero count}
_ZERO_STREAKS: dict[str, dict[int, int]] = {}


def classify_probe_reading(session_id: str, address: int, value: int) -> tuple[str, str]:
    """Record one probe reading and classify it.

    Returns ``(value_status, note)``. ``note`` is '' unless the reading is
    suspected stale. Read failures do NOT go through here -- a failed read
    has its own status ('failed'); this only classifies successful reads.

    This is the ONLY place the streak state is mutated, so `smoke.py`
    (health's game_mode probe) and `state_observer.py` (the observe action)
    share one history instead of each keeping a private, drifting copy.
    """
    streaks = _ZERO_STREAKS.setdefault(session_id, {})
    if value != 0:
        streaks.pop(address, None)
        return VALUE_OK, ""
    streaks[address] = streaks.get(address, 0) + 1
    if streaks[address] >= STALE_ADDRESS_STREAK:
        return VALUE_STALE_SUSPECTED, (
            f"address 0x{address:08X} has read zero {streaks[address]} times in a row "
            f"(>= {STALE_ADDRESS_STREAK}); suspect the probe address is stale -- "
            f"this is a suspicion, not a verdict: the value may genuinely be zero"
        )
    return VALUE_OK, ""


def reset_probe_streaks(session_id: str) -> None:
    """Drop the streak history for a session (call when it ends)."""
    _ZERO_STREAKS.pop(session_id, None)
