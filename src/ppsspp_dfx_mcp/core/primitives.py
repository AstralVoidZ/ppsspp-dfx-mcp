"""Shared domain primitives (frame timing + memory budgets).

Single home for the few constants that the lower layers (core, service)
enforce alongside the tools layer, so the dependency direction stays
one-way: tools may import core; core/service never import tools.

Import these; do not copy the literals — duplicated budgets are how
tool/client limits drift apart.
"""

from __future__ import annotations

# ── Frame-wait pacing (input.wait_frames + batch_step wait steps) ────────
# PPSSPP semantics: 1 frame = 1/60 s.

DEFAULT_FRAME_INTERVAL_S = 1.0 / 60.0

# ── Memory read/write limits ─────────────────────────────────────────────

MAX_SINGLE_READ_BYTES = 65536   # hard ceiling for one memory.read
