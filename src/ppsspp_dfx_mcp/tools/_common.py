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
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ppsspp_dfx_mcp.config import output_dir
from ppsspp_dfx_mcp.core.primitives import (
    DEFAULT_FRAME_INTERVAL_S,
    MAX_SINGLE_READ_BYTES,  # noqa: F401 — tool-layer re-export hub (R9 contract)
    MAX_WAIT_FRAMES,
)
from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError, to_tool_error

logger = logging.getLogger(__name__)

# ── Schema governance: registered exceptions to `tool-schema-contract` ────
# Parameters whose shape is decided by *runtime data* and therefore cannot
# be constrained statically. Each entry is `<tool_name>.<param_name>` and
# MUST be argued for in the `tool-schema-contract` spec's exception clause
# before being listed here.
#
# Single source of truth on purpose: the guard test
# (`tests/unit/l2_mcp_contract/test_output_schema_contract.py`) and the
# gate script (`scripts/report_schema_surface.py`) both import this set —
# if only one of them knew about an exemption, the other would report a
# violation forever (the script's exit code is the CI signal).
DYNAMIC_INPUT_PARAMETERS: frozenset[str] = frozenset(
    {
        # Shape depends on the called script (each diagnostic script
        # declares its own Input model); callers must first learn the
        # target via ppsspp_list_scripts.
        "ppsspp_run_script.input",
    }
)

# ── Schema governance: tools whose return shape varies by branch ─────────
# The SDK validates every returned dict against the derived output contract
# (`func_metadata.convert_result`, see `views/_contract.py` docstring): a
# missing required field is a hard tool error. A tool that returns a
# *different view* per branch therefore cannot use a single-shape contract —
# its contract must be derived with `partial=True`.
#
# Each entry is `<tool_name>` -> why it has more than one shape. The guard
# test `test_multi_shape_tools_are_registered` walks every registered tool's
# source with AST and fails when a tool constructs ≥2 `*Response` classes but
# is absent here. Without that guard the failure mode is a runtime tool
# error on one branch only — invisible to a test suite that does not call
# that branch.
MULTI_SHAPE_OUTPUT_TOOLS: dict[str, str] = {
    "ppsspp_diff_memory": (
        "action 分发：snapshot/compare/drop/list 各返回不同视图"
        "（partial=True 全字段可选）"
    ),
    "ppsspp_batch_status": (
        "batch_id 省略（survey/list 模式）返回 BatchListResponse"
        "（{jobs, retention_jobs}），指定 batch_id 返回 BatchStatusResponse"
        "（{batch_id, status, ...}）— v0.1.6 合并 ppsspp_batch_list"
    ),
    "ppsspp_session": (
        "action='wait_ready' 返回 WaitReadyResponse（{action, ready, elapsed_s, "
        "probe_addr, probe_value, note}），其余 action 返回 SessionResponse"
    ),
    "ppsspp_batch_step": (
        "background=True 返回 BatchSubmitResponse（{action, batch_id, ...}），"
        "前台分支返回 BatchStepResponse（{executed, succeeded, results, ...}）"
    ),
    "ppsspp_frame_snapshot": (
        "无会话/不可暂停路径返回 StateObserverResponse，正常路径返回 FrameSnapshotResponse"
    ),
}

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
MIN_SCAN_CHUNK_BYTES = 64  # scan chunk lower clamp
MAX_SCAN_RANGE_BYTES = 256 * 1024 * 1024  # scan range upper cap
# Scan reads chunk + len(pattern)-1 bytes in ONE memory.read, so an
# unbounded pattern silently exceeds the 64 KiB single-read budget the
# tool layer documents (symptom: an "empty successful scan"). Real-PPSSPP
# probes showed 44KiB reads succeed, so this is a contract-consistency
# cap, not a hard server limit.
MAX_SCAN_PATTERN_BYTES = 4096

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
        frames: game frames to wait; 0..MAX_WAIT_FRAMES.
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


def require_session_id(session_id: str | None) -> str:
    """Guard: raise the canonical ToolError when session_id is missing.

    The message and code are the canonical error contract shared by all
    tools — do not vary them per call site.
    """
    if not session_id:
        raise ArgsInvalid("session_id is required")
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
        raise ArgsInvalid(f"resolved path escapes output dir: {path}")
    return path


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
    return str(path)


async def save_output_text(subdir: str, filename: str, text: str) -> str:
    """Save ``text`` to ``output/{subdir}/{filename}`` off the event loop."""
    path = resolve_output_path(subdir, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(path.write_text, text, encoding="utf-8")
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
