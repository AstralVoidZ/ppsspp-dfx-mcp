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

# ── Scan budget guard (v0.1.7, real-PPSSPP evidence 2026-09-30) ──────────
# Lives in core (not tools/_common) because service/debug_client's
# scan_memory enforces the per-chunk timeout, and the layering tripwire
# forbids service → tools imports.
#
# Foreground scans run inside the MCP request scope; the ZCode client
# times tools out at ~30s. Measured: 24 MB @ 4 KiB chunks ≈ 53-96 s
# (build-dependent) — far past that budget, and the client-cancel/retry
# loop is what *looks* like a frozen session. Beyond this soft limit the
# scan tool auto-submits as a background job instead of failing.
FOREGROUND_SCAN_LIMIT_BYTES = 2 * 1024 * 1024
# A healthy localhost WS read of ≤64 KiB is single-digit ms (measured
# ~9 ms/chunk on v1.20.4-605). A 10 s timeout means the emulator is
# wedged, not slow; after SCAN_MAX_CONSECUTIVE_READ_FAILURES consecutive
# timeouts the scan aborts instead of silently skipping (which would let
# a hung PPSSPP pin the session lock forever).
SCAN_READ_TIMEOUT_S = 10.0
SCAN_MAX_CONSECUTIVE_READ_FAILURES = 5
# Wall-clock budget for one background scan job (detached task). A job
# that exceeds it is failed cleanly so the session lock is always
# released — the "only stop/restart helps" wedge from the field report.
SCAN_BG_BUDGET_S = 600.0
