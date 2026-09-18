"""Diff memory view — public JSON contract for ppsspp_diff_memory."""

from __future__ import annotations

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.models.diff import (
    DiffCompareResult,
    DiffSnapshotResult,
)
from ppsspp_dfx_mcp.views._base import FrozenModel


class DiffSnapshotResponse(FrozenModel):
    """Response view for action=snapshot."""

    handle: str = Field(description="Snapshot handle for compare/drop.")
    start: str = Field(description="Start address (hex).")
    size_bytes: int = Field(description="Snapshot size in bytes.")
    checksum: str = Field(description="sha256 of the snapshot bytes (first 16 hex).")

    @classmethod
    def from_result(cls, result: DiffSnapshotResult) -> DiffSnapshotResponse:
        return cls(
            handle=result.handle,
            start=format_address(result.start),
            size_bytes=result.size,
            checksum=result.checksum,
        )


class DiffChangeView(FrozenModel):
    """One changed byte."""

    address: str = Field(description="Address of the changed byte (hex).")
    old: int = Field(description="Snapshot byte value.")
    new: int = Field(description="Current byte value.")


class DiffCompareResponse(FrozenModel):
    """Response view for action=compare."""

    handle: str = Field(description="Compared snapshot handle.")
    start: str = Field(description="Start address (hex).")
    size_bytes: int = Field(description="Compared range size in bytes.")
    changed_count: int = Field(description="Total changed bytes.")
    truncated: bool = Field(
        description="True when changes exceed the inline cap (see changes list).",
    )
    changes: list[DiffChangeView] = Field(
        default_factory=list,
        description="First N changed bytes (address + old/new).",
    )

    @classmethod
    def from_result(cls, result: DiffCompareResult) -> DiffCompareResponse:
        return cls(
            handle=result.handle,
            start=format_address(result.start),
            size_bytes=result.size,
            changed_count=result.changed_count,
            truncated=result.truncated,
            changes=[
                DiffChangeView(address=format_address(c.address), old=c.old, new=c.new)
                for c in result.changes
            ],
        )


class DiffDropResponse(FrozenModel):
    """Response view for action=drop."""

    handle: str = Field(description="Dropped handle.")
    dropped: bool = Field(description="True when the handle existed and was removed.")

    @classmethod
    def build(cls, handle: str, dropped: bool) -> DiffDropResponse:
        return cls(handle=handle, dropped=dropped)


class DiffListResponse(FrozenModel):
    """Response view for action=list."""

    handles: list[DiffSnapshotResponse] = Field(
        default_factory=list,
        description="Live snapshot handles (oldest first).",
    )
    count: int = Field(description="Number of live snapshots.")
    max_snapshots: int = Field(description="Registry capacity (FIFO eviction).")

    @classmethod
    def build(cls, results: list[DiffSnapshotResult], max_snapshots: int) -> DiffListResponse:
        views = [DiffSnapshotResponse.from_result(r) for r in results]
        return cls(handles=views, count=len(views), max_snapshots=max_snapshots)


__all__ = [
    "DiffCompareResponse",
    "DiffDropResponse",
    "DiffListResponse",
    "DiffSnapshotResponse",
]
