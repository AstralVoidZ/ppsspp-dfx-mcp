"""Diff memory tool — snapshot / compare / drop / list for byte-level change
detection across a memory range.

Pure client-side orchestration over chunked `memory.read` calls (no new WS
events). Typical loop: snapshot → play/act → compare → changed-address list.
Snapshots live in a bounded in-memory registry (FIFO, `_MAX_SNAPSHOTS`);
registry state is per-server-process and intentionally not persisted.
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.core.primitives import MAX_SINGLE_READ_BYTES
from ppsspp_dfx_mcp.errors import ArgsInvalid, to_tool_error
from ppsspp_dfx_mcp.models.diff import (
    DiffChange,
    DiffCompareResult,
    DiffSnapshotResult,
)
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import resolve_session_id, session_client
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.diff import (
    DiffCompareResponse,
    DiffDropResponse,
    DiffListResponse,
    DiffSnapshotResponse,
)

logger = logging.getLogger(__name__)

_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024  # 8 MiB per snapshot
_MAX_SNAPSHOTS = 4  # FIFO eviction
_MAX_CHANGES_INLINE = 256  # changes beyond this are truncated (count kept)

DiffOutput = derive_output_contract(
    "DiffOutput",
    DiffSnapshotResponse,
    partial=True,  # multi-shape: snapshot/compare/drop/list return different views
)

# handle -> (result meta, snapshot bytes, owning session_id)
_SNAPSHOTS: dict[str, tuple[DiffSnapshotResult, bytes, str]] = {}


def _reset_registry_for_tests() -> None:
    """Test isolation hook — clears the snapshot registry."""
    _SNAPSHOTS.clear()


def _evict_oldest_if_full() -> None:
    while len(_SNAPSHOTS) >= _MAX_SNAPSHOTS:
        oldest = next(iter(_SNAPSHOTS))
        _SNAPSHOTS.pop(oldest)
        logger.info("diff snapshot evicted (FIFO): %s", oldest)


async def _read_segments(client: Any, start: int, size: int) -> bytes:
    """Read [start, start+size) skipping unreadable chunks (pad 0x00).

    Consistent with scan's unreadable-region contract: the buffer is
    always `size` bytes long, but chunks that fail to read are left as
    null bytes. Compare diff results may contain false positives in
    skipped regions — callers should narrow ranges to known-readable
    areas for precise results.
    """
    data = bytearray(size)
    for offset in range(0, size, MAX_SINGLE_READ_BYTES):
        chunk = min(MAX_SINGLE_READ_BYTES, size - offset)
        try:
            raw = await client.read_bytes(address=start + offset, size=chunk)
            data[offset : offset + len(raw)] = raw
        except Exception:
            pass  # skip unreadable chunk (scan contract alignment)
    return bytes(data)


@mcp.tool(
    name="ppsspp_diff_memory",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False
    ),
)
@translate_tool_errors
async def diff_memory(
    action: Annotated[
        Literal["snapshot", "compare", "drop", "list"],
        Field(
            description=(
                "Diff operation. Valid values:\n"
                "- 'snapshot': read [start, start+size) and store it under a "
                "new handle (registry cap 4, FIFO eviction).\n"
                "- 'compare': read the same range now and diff against the "
                "handle's snapshot — returns changed-byte list "
                "(inline cap 256, truncated flag keeps the true count).\n"
                "- 'drop': release a handle.\n"
                "- 'list': live handles."
            ),
        ),
    ],
    session_id: Annotated[
        str | None,
        Field(
            description=(
                "Active session ID; auto-resolved when exactly one session "
                "is active (required for snapshot/compare, ignored for "
                "drop/list)."
            ),
        ),
    ] = None,
    start: Annotated[
        str | None,
        Field(
            description=("Range start, hex ('0x08804000') or decimal — required for snapshot."),
        ),
    ] = None,
    end: Annotated[
        str | None,
        Field(
            description="Range end (exclusive), same format — required for snapshot.",
        ),
    ] = None,
    handle: Annotated[
        str | None,
        Field(
            description="Snapshot handle — required for compare/drop.",
        ),
    ] = None,
) -> DiffOutput:
    """PURPOSE: Snapshot a memory range and diff it against current memory — the classic "what changed?" variable-locator.

    USAGE: diff_memory(action="snapshot", start=..., end=...) → handle; act in game; diff_memory(action="compare", handle=...) → changed-byte list; action="drop"/"list" manage handles.

    BEHAVIOR: READ-ONLY. Memory is never written — only the per-server snapshot registry mutates. Large ranges are read across multiple reads; per-snapshot cap 8 MiB; registry cap 4 with FIFO eviction. compare requires the handle's exact range.

    RETURNS: snapshot → {handle, start, size_bytes, checksum}; compare → {handle, start, size_bytes, changed_count, truncated, changes: [{address, old, new}]}; drop → {handle, dropped}; list → {handles: [...], count, max_snapshots}."""
    if action not in ("snapshot", "compare", "drop", "list"):
        raise ArgsInvalid(f"invalid action={action!r}")
    logger.info("tool_call", extra={"tool": "ppsspp_diff_memory", "action": action})

    try:
        if action == "snapshot":
            if start is None or end is None:
                raise ArgsInvalid("action='snapshot' requires start and end")
            start_int = parse_address(start)
            end_int = parse_address(end)
            if start_int <= 0 or end_int <= start_int:
                raise ArgsInvalid(
                    f"invalid range: start={start!r} end={end!r} — need 0 < start < end"
                )
            size = end_int - start_int
            if size > _MAX_SNAPSHOT_BYTES:
                raise ArgsInvalid(
                    f"range size {size} bytes exceeds the per-snapshot cap "
                    f"{_MAX_SNAPSHOT_BYTES} — narrow start/end"
                )
            session_id_resolved = await resolve_session_id(session_id)
            async with session_client(session_id_resolved) as client:
                data = await _read_segments(client, start_int, size)
            _evict_oldest_if_full()
            handle = uuid.uuid4().hex[:8]
            result = DiffSnapshotResult(
                handle=handle,
                start=start_int,
                size=size,
                checksum=hashlib.sha256(data).hexdigest()[:16],
                created_at=time.time(),
            )
            _SNAPSHOTS[handle] = (result, data, session_id_resolved)
            return DiffSnapshotResponse.from_result(result).model_dump(mode="json")

        if action == "compare":
            if not handle:
                raise ArgsInvalid("action='compare' requires handle")
            entry = _SNAPSHOTS.get(handle)
            if entry is None:
                raise ArgsInvalid(
                    f"unknown handle {handle!r} — use action='list' "
                    f"(handles are per-server-process and FIFO-evicted)"
                )
            meta, snap_bytes, snap_session = entry
            session_id_resolved = await resolve_session_id(session_id)
            if snap_session != session_id_resolved:
                raise ArgsInvalid(
                    f"handle {handle!r} was snapshotted in session "
                    f"{snap_session!r}, not {session_id_resolved!r} — "
                    f"cross-session comparison would produce a meaningless "
                    f"diff; snapshot it again in this session"
                )
            async with session_client(session_id_resolved) as client:
                current = await _read_segments(client, meta.start, meta.size)
            changes: list[DiffChange] = []
            truncated = False
            for offset, (old, new) in enumerate(zip(snap_bytes, current, strict=True)):
                if old != new:
                    if len(changes) < _MAX_CHANGES_INLINE:
                        changes.append(DiffChange(meta.start + offset, old, new))
                    else:
                        truncated = True
            result = DiffCompareResult(
                handle=handle,
                start=meta.start,
                size=meta.size,
                changed_count=sum(1 for o, n in zip(snap_bytes, current, strict=True) if o != n),
                truncated=truncated,
                changes=tuple(changes),
            )
            return DiffCompareResponse.from_result(result).model_dump(mode="json")

        if action == "drop":
            if not handle:
                raise ArgsInvalid("action='drop' requires handle")
            dropped = _SNAPSHOTS.pop(handle, None) is not None
            return DiffDropResponse.build(handle, dropped).model_dump(mode="json")

        # action == "list"
        return DiffListResponse.build(
            [meta for meta, _, _ in _SNAPSHOTS.values()], _MAX_SNAPSHOTS
        ).model_dump(mode="json")
    except Exception as e:
        raise to_tool_error(e) from e
