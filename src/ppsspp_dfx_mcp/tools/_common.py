"""Shared tool-layer plumbing: output-path helpers, session guard, error
translation, and shared frame-wait pacing constants (internal — registers
no MCP tools).

Key contracts:
- Output paths: callers only influence the FILENAME; the directory is
  always ``.ppsspp-dfx/output/{subdir}/``, and disk writes run off the
  event loop so multi-MB writes never stall it.
- ``translate_tool_errors`` is signature-preserving: the SDK generates
  inputSchema via ``inspect.signature(fn, eval_str=True)``, which follows
  ``__wrapped__`` back to the original function.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ppsspp_dfx_mcp.config import output_dir
from ppsspp_dfx_mcp.core.primitives import (
    DEFAULT_FRAME_INTERVAL_S,
    FOREGROUND_SCAN_LIMIT_BYTES,  # noqa: F401 — tool-layer re-export hub
    MAX_SINGLE_READ_BYTES,  # noqa: F401 — tool-layer re-export hub
    MAX_WAIT_FRAMES,
    SCAN_BG_BUDGET_S,  # noqa: F401 — tool-layer re-export hub
    SCAN_MAX_CONSECUTIVE_READ_FAILURES,  # noqa: F401 — tool-layer re-export hub
    SCAN_READ_TIMEOUT_S,  # noqa: F401 — tool-layer re-export hub
)
from ppsspp_dfx_mcp.core.value_staleness import (
    _ZERO_STREAKS,  # noqa: F401 — tool-layer re-export hub
    STALE_ADDRESS_STREAK,  # noqa: F401 — tool-layer re-export hub
    VALUE_OK,  # noqa: F401 — tool-layer re-export hub
    VALUE_STALE_SUSPECTED,  # noqa: F401 — tool-layer re-export hub
    classify_probe_reading,  # noqa: F401 — tool-layer re-export hub
    reset_probe_streaks,  # noqa: F401 — tool-layer re-export hub
)
from ppsspp_dfx_mcp.errors import ArgsInvalid, SessionNotFound, ToolError, to_tool_error
from ppsspp_dfx_mcp.spec.tool_surface_policy import (
    CONDITIONAL_REQUIRED_PARAMS,  # noqa: F401 — tool-layer re-export hub
    DESTRUCTIVE_HINT_POLICY,  # noqa: F401 — tool-layer re-export hub
    DESTRUCTIVE_TOOLS,  # noqa: F401 — tool-layer re-export hub
    DYNAMIC_INPUT_PARAMETERS,  # noqa: F401 — tool-layer re-export hub
    MULTI_SHAPE_OUTPUT_TOOLS,  # noqa: F401 — tool-layer re-export hub
    NON_DESTRUCTIVE_BY_POLICY,  # noqa: F401 — tool-layer re-export hub
    REMOVAL_ACTIONS,  # noqa: F401 — tool-layer re-export hub
)

logger = logging.getLogger(__name__)

# ── Re-export hub (governance registries + staleness state machine) ──────
# The tool-surface governance registry now lives in `spec/tool_surface_policy.py`
# and the stale-address suspicion state machine in `core/value_staleness.py`
# (both imported above). They are re-exported here so every historical import
# point — the tool modules, the guard tests and the gate script
# `scripts/report_schema_surface.py` — keeps resolving them from this hub.

# ── Frame-wait pacing (input.wait_frames + batch_step wait steps) ────────
# DEFAULT_FRAME_INTERVAL_S / MAX_WAIT_FRAMES live in core.primitives
# (imported above), shared with models/batch_step descriptions — import;
# do not copy the literals.

# Bounds for interval validation: 0 < interval <= 1s (1s per frame is
# already 60x slower than realtime; anything above is a units mistake).
MIN_FRAME_INTERVAL_S = 0.0001
MAX_FRAME_INTERVAL_S = 1.0

# ── Memory read/write limits (single source of truth) ────────────────────
# MAX_SINGLE_READ_BYTES lives in core.primitives (imported above) — the
# service layer enforces the same budget. duplicating the literals is how
# tool/client budgets drift. Import; do not copy.
DEFAULT_STRING_CAP = 4096  # read_string default when max_len <= 0
# The scan bounds (MIN_SCAN_CHUNK_BYTES / MAX_SCAN_RANGE_BYTES /
# MAX_SCAN_PATTERN_BYTES) moved to `service/scan_engine.py` together with
# the pattern-scan algorithm they bound (W19). Import them from there.

# ── Log-analysis limits ──────────────────────────────────────────────────

MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MiB — PPSSPP session logs stay < 1 MiB
MAX_LOG_MATCHES = 500


_LIVENESS_CHUNK_FRAMES = 60  # ~1s at 60 FPS between liveness checks


async def wait_frames_chunked(
    frames: int,
    interval_s: float | None,
    session_id: str | None = None,
) -> float:
    """Chunked frame wait with mid-sleep session-liveness checks.

    Shared by ``input.wait_frames`` and ``batch_step`` wait steps so both
    paths validate frames/interval identically (interval <= 0 would
    silently become sleep(0) or a hot loop hammering sessions.json) and
    check session liveness between chunks.

    Args:
        frames: game frames to wait; 0..MAX_WAIT_FRAMES. Note 0 is a
            legitimate no-op for ``batch_step`` wait steps (R12 ratified),
            so the ``>= 1`` rule for the ``wait_frames`` TOOL is enforced in
            that tool's own body (G-10 / FR-010), not here — tightening the
            shared waiter would reject batches that legitimately no-op.
        interval_s: per-frame seconds; None → DEFAULT_FRAME_INTERVAL_S;
            must be in [MIN_FRAME_INTERVAL_S, MAX_FRAME_INTERVAL_S].
        session_id: when given, the session is validated before sleeping
            and re-validated between chunks (surfaces session death
            promptly instead of sleeping the full duration).

    Returns:
        Elapsed wall-clock seconds.

    Raises:
        ToolError: invalid frames/interval (fail-fast, before any sleep).
        SessionNotFound / SessionExpired: propagated from liveness checks
            (the caller's error translation wraps them).
    """
    if isinstance(frames, bool) or not isinstance(frames, int) or frames < 0:
        raise ArgsInvalid(f"frames must be int >= 0; got {frames!r}")
    if frames > MAX_WAIT_FRAMES:
        raise ArgsInvalid(
            f"frames {frames} exceeds the cap {MAX_WAIT_FRAMES} "
            f"(~{MAX_WAIT_FRAMES // 60}s of game time)"
        )
    per_frame = interval_s if interval_s is not None else DEFAULT_FRAME_INTERVAL_S
    if isinstance(per_frame, bool) or not isinstance(per_frame, (int, float)):
        raise ArgsInvalid(f"interval must be a number of seconds; got {per_frame!r}")
    if not (MIN_FRAME_INTERVAL_S <= per_frame <= MAX_FRAME_INTERVAL_S):
        raise ArgsInvalid(
            f"interval must be in [{MIN_FRAME_INTERVAL_S}, "
            f"{MAX_FRAME_INTERVAL_S}] seconds; got {per_frame!r} "
            "(interval <= 0 would busy-loop the event loop)"
        )

    if session_id is not None:
        from ppsspp_dfx_mcp.session.client_helper import validate_session_alive

        await validate_session_alive(session_id)

    start = time.monotonic()
    remaining = frames
    while remaining > 0:
        chunk = min(_LIVENESS_CHUNK_FRAMES, remaining)
        await asyncio.sleep(chunk * per_frame)
        remaining -= chunk
        if session_id is not None and remaining > 0:
            from ppsspp_dfx_mcp.session.client_helper import validate_session_alive

            await validate_session_alive(session_id)
    return time.monotonic() - start


def require_int_not_bool(value: Any, name: str, *, exc: type[ToolError] = ArgsInvalid) -> int:
    """Guard: reject ``bool`` where an ``int`` parameter is expected.

    ``isinstance(True, int)`` is True in Python, so an unguarded check let
    ``size=True`` mean a 1-byte read and ``count=True`` a 1-step batch. The
    caller passes the public parameter name (and, for step validation, the
    step-local error class) so the message points at the offending argument.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise exc(f"{name} must be an int (bool is not accepted); got {value!r}")
    return value


def require_session_id(session_id: str | None) -> str:
    """Guard an explicitly-passed session_id for schema-required tools.

    The counterpart of ``resolve_session_id`` (FR-001) for tools whose
    ``session_id`` is schema-required: they cannot omit it, so omission
    never reaches the resolver. An empty value means "no session named"
    and raises the SAME ``SessionNotFound`` (``SESSION_NOT_FOUND``) every
    session-aware tool returns — including the resolver's 0-session case —
    so the whole surface answers "which session?" with one error code.

    Existence of a non-empty id is enforced downstream by
    ``session_client``, which raises ``SessionNotFound`` for an unknown id.
    """
    if not session_id:
        raise SessionNotFound(
            "session_id is required — start a session with "
            'ppsspp_session(action="start", iso_path=...)'
        )
    return session_id


def resolve_output_path(subdir: str, filename: str) -> Path:
    """Containment-resolved path under ``.ppsspp-dfx/output/{subdir}/``.

    The directory component is ALWAYS server-chosen; the caller may only
    influence the file name. Any path separator or ``..`` component in
    ``filename`` is rejected, so a prompt-injected ``file_path`` can never
    make a tool write or read outside the output tree.

    Raises:
        ToolError: ``filename`` is empty, contains directory parts, or
            the resolved path escapes the output root.
    """
    if not filename or Path(filename).name != filename:
        raise ArgsInvalid(
            f"filename must be a bare file name without directory parts: {filename!r}"
        )
    root = (output_dir() / subdir).resolve()
    path = (root / filename).resolve()
    if not path.is_relative_to(root):
        # Defense in depth — unreachable after the name check on POSIX
        # and Windows, but keeps the invariant explicit.
        # Review-v4 W-6: no resolved (server-derived) path in the message.
        raise ArgsInvalid(f"filename {filename!r} resolves outside the output directory")
    return path


def _chmod_owner_only(path: Path) -> None:
    """Best-effort POSIX 0o600 on a freshly written output file.

    Raw memory dumps / screenshots / recordings land under
    ``.ppsspp-dfx/output/`` and must be owner-only, matching the
    ``sessions.json`` 0o600 standard. Windows ACLs are left untouched.
    """
    if os.name != "posix":
        return
    # Permission tightening is best-effort: a filesystem that rejects
    # chmod must not fail an otherwise successful capture.
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


async def save_output_bytes(subdir: str, filename: str, data: bytes) -> str:
    """Save ``data`` to ``output/{subdir}/{filename}`` off the event loop.

    Returns the absolute file path string. Raises ToolError on illegal
    filenames (see ``resolve_output_path``); propagates OSError from the
    write (callers decide how to surface it — e.g. replay.save embeds the
    recording payload in the error).
    """
    path = resolve_output_path(subdir, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_bytes, data)
    _chmod_owner_only(path)
    return str(path)


async def save_output_text(subdir: str, filename: str, text: str) -> str:
    """Save ``text`` to ``output/{subdir}/{filename}`` off the event loop."""
    path = resolve_output_path(subdir, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, text, encoding="utf-8")
    _chmod_owner_only(path)
    return str(path)


def translate_tool_errors[F: Callable[..., Awaitable[Any]]](fn: F) -> F:
    """Wrap an async tool: ToolError passes through, everything else is
    translated via ``to_tool_error``.

    Replaces the verbatim two-branch ``try/except`` that closed out ~23
    tool bodies. ``functools.wraps`` keeps the SDK's signature
    introspection working (see module docstring). Only for async tools —
    the two sync tools (ppsspp_list_scripts / ppsspp_reload_scripts) keep
    their explicit try/except.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as e:
            raise to_tool_error(e) from e

    return wrapper  # type: ignore[return-value]
