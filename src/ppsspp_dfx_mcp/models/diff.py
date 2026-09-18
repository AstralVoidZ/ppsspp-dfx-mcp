"""Diff memory models — result dataclasses for ppsspp_diff_memory.

Pure client-side orchestration over chunked `memory.read` calls; no new
WS events. Snapshots live in a bounded in-memory registry (tools/diff.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DiffSnapshotResult:
    """A stored snapshot handle."""

    handle: str
    start: int
    size: int
    checksum: str
    created_at: float


@dataclass(frozen=True)
class DiffChange:
    """One changed byte (address + old/new value at that byte)."""

    address: int
    old: int
    new: int


@dataclass(frozen=True)
class DiffCompareResult:
    """Compare outcome between a stored snapshot and current memory."""

    handle: str
    start: int
    size: int
    changed_count: int
    truncated: bool
    changes: tuple[DiffChange, ...] = field(default_factory=tuple)
