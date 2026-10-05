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
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.core.primitives import MAX_SINGLE_READ_BYTES
from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.models.diff import (
    DiffChange,
    DiffCompareResult,
    DiffSnapshotResult,
)
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.session.client_helper import resolve_session_id, session_client
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract, flatten_union
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
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

_DiffSnapshotOut = derive_output_contract("DiffSnapshotOut", DiffSnapshotResponse, partial=True)
_DiffCompareOut = derive_output_contract("DiffCompareOut", DiffCompareResponse, partial=True)
_DiffDropOut = derive_output_contract("DiffDropOut", DiffDropResponse, partial=True)
_DiffListOut = derive_output_contract("DiffListOut", DiffListResponse, partial=True)


# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    DiffOutput = dict[str, Any]
else:
    DiffOutput = flatten_union(
        "DiffOutput", _DiffSnapshotOut, _DiffCompareOut, _DiffDropOut, _DiffListOut
    )
# handle -> (result meta, snapshot bytes, owning session_id)
_SNAPSHOTS: dict[str, tuple[DiffSnapshotResult, bytes, str]] = {}


def reset_snapshots() -> None:
    """清空快照注册表（语义化回收 API；长驻进程的内存回收面）。"""
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


# LONG-TOOL: the snapshot registry lifecycle (snapshot/compare/drop/list) and the multi-read capture
# path share one process-global bounded registry, so they are kept in a single tool body.
@mcp.tool(
    name="ppsspp_diff_memory",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=False
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
                "handle's snapshot; returns a changed-byte list "
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
            description=(
                "Range start, hex ('0x08804000') or decimal — required for "
                "snapshot unless 'address'+'size' are given."
            ),
        ),
    ] = None,
    end: Annotated[
        str | None,
        Field(
            description=(
                "Range end (exclusive), same format — required for snapshot "
                "unless 'address'+'size' are given."
            ),
        ),
    ] = None,
    address: Annotated[
        str | None,
        Field(
            description=(
                "Range start, same format as 'start'. Provided for naming "
                "consistency with read_memory/write_memory/scan, which all "
                "take 'address'. Use either address+size or start+end."
            ),
        ),
    ] = None,
    size: Annotated[
        int | None,
        Field(
            description=(
                "Range length in bytes. Provided for naming consistency with "
                "read_memory/write_memory/scan, which all take 'size'. Use "
                "either address+size or start+end."
            ),
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

    BEHAVIOR: READ-ONLY. Memory is never written — only the per-server snapshot registry mutates. Large ranges are read across multiple reads; per-snapshot cap 8 MiB; registry cap 4 with FIFO eviction. The registry is process-global: parallel sessions share one cap and FIFO order, so another session's snapshots can evict yours under load. compare requires the handle's exact range.

    RETURNS: snapshot → {handle, start, size_bytes, checksum}; compare → {handle, start, size_bytes, changed_count, truncated, changes: [{address, old, new}]}; drop → {handle, dropped}; list → {handles: [...], count, max_snapshots}."""
    if action not in ("snapshot", "compare", "drop", "list"):
        raise ArgsInvalid(f"invalid action={action!r}")
    logger.info("tool_call", extra={"tool": "ppsspp_diff_memory", "action": action})

    # Session resolution up-front for session-touching actions, matching
    # ppsspp_scan's precedence (session errors before action-specific ones).
    # Every valid action resolves (the set is validated above): drop/list
    # resolve too — ownership of a snapshot must be enforced on every action
    # that touches it, under the SAME resolved identity the snapshot stored.
    session_id_resolved = await resolve_session_id(session_id)

    if action == "snapshot":
        # accept address+size as well as start+end. Every other
        # memory tool in this server (read_memory, write_memory, scan)
        # names the start 'address', so requiring 'start' here made the
        # family inconsistent and cost a blind caller a round trip. The
        # error below names both forms and shows an example.
        if (start is not None or end is not None) and (address is not None or size is not None):
            # A-7 (review v4): start (without end) + address+size passed
            # the partial-form check and the start was silently dropped.
            raise ArgsInvalid(
                "action='snapshot' accepts ONE range form — either "
                "start+end or address+size, not both"
            )
        if (start is None) != (end is None) and (address is None or size is None):
            raise ArgsInvalid(
                "action='snapshot' needs a range, given either as "
                "start+end (e.g. start='0x09000000', end='0x09000040') or "
                "as address+size (e.g. address='0x09000000', size=64)"
            )
        if start is not None and end is not None:
            range_start, range_end = start, end
        elif address is not None and size is not None:
            if size <= 0:
                raise ArgsInvalid(f"size must be > 0 (got {size}) when using address+size")
            range_start = address
            range_end = hex(parse_address(address) + size)
        else:
            raise ArgsInvalid(
                "action='snapshot' needs a range, given either as "
                "start+end (e.g. start='0x09000000', end='0x09000040') or "
                "as address+size (e.g. address='0x09000000', size=64)"
            )
        start_int = parse_address(range_start)
        end_int = parse_address(range_end)
        if start_int <= 0 or end_int <= start_int:
            raise ArgsInvalid(f"invalid range: start={start!r} end={end!r} — need 0 < start < end")
        span = end_int - start_int  # A-7: not named `size` — that used
        # to shadow the caller's address+size form parameter below.
        if span > _MAX_SNAPSHOT_BYTES:
            raise ArgsInvalid(
                f"range size {span} bytes exceeds the per-snapshot cap "
                f"{_MAX_SNAPSHOT_BYTES} — narrow start/end"
            )
        async with session_client(session_id_resolved) as client:
            data = await _read_segments(client, start_int, span)
        _evict_oldest_if_full()
        handle = uuid.uuid4().hex[:8]
        result = DiffSnapshotResult(
            handle=handle,
            start=start_int,
            size=span,
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
        if snap_session != session_id_resolved:
            raise ArgsInvalid(
                f"handle {handle!r} was snapshotted in session "
                f"{snap_session!r}, not {session_id_resolved!r} — "
                f"cross-session comparison would produce a meaningless "
                f"diff; snapshot it again in this session"
            )
        async with session_client(session_id_resolved) as client:
            current = await _read_segments(client, meta.start, meta.size)
        # Single pass — the old code walked both
        # buffers a second time just to count, i.e. up to 2×8MiB
        # Python-level iterations while blocking the event loop.
        changed_count = 0
        changes: list[DiffChange] = []
        truncated = False
        for offset, (old, new) in enumerate(zip(snap_bytes, current, strict=True)):
            if old == new:
                continue
            changed_count += 1
            if len(changes) < _MAX_CHANGES_INLINE:
                changes.append(DiffChange(meta.start + offset, old, new))
            else:
                truncated = True
        compare_result = DiffCompareResult(
            handle=handle,
            start=meta.start,
            size=meta.size,
            changed_count=changed_count,
            truncated=truncated,
            changes=tuple(changes),
        )
        return DiffCompareResponse.from_result(compare_result).model_dump(mode="json")

    if action == "drop":
        if not handle:
            raise ArgsInvalid("action='drop' requires handle")
        entry = _SNAPSHOTS.get(handle)
        if entry is not None:
            _, _, snap_session = entry
            if snap_session != session_id_resolved:
                # Same ownership contract as compare —
                # dropping another session's snapshot from session A
                # would make session B's next compare fail confusingly.
                raise ArgsInvalid(
                    f"handle {handle!r} was snapshotted in session "
                    f"{snap_session!r}, not {session_id_resolved!r}"
                )
        dropped = _SNAPSHOTS.pop(handle, None) is not None
        return DiffDropResponse.build(handle, dropped).model_dump(mode="json")

    # action == "list"
    return DiffListResponse.build(
        [meta for meta, _, _ in _SNAPSHOTS.values()], _MAX_SNAPSHOTS
    ).model_dump(mode="json")
