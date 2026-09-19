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

# Upper bounds so a mistyped/malicious wait or press cannot hang the tool
# (or busy-loop the event loop) indefinitely. 5 minutes of game frames.
# models/batch_step.py interpolates these into field descriptions — do not
# restate the number there or in any doc text.
MAX_WAIT_FRAMES = 60 * 300
MAX_PRESS_DURATION_FRAMES = 60 * 300

# ── Memory read/write limits ─────────────────────────────────────────────

MAX_SINGLE_READ_BYTES = 65536  # hard ceiling for one memory.read

# Symmetric write cap (W10, review v2): memory.write payloads travel the
# same WS frame as reads — unbounded base64 writes hit the same
# transport limits. Callers chunk. Import; do not copy.
MAX_WRITE_BYTES = 0x10000
