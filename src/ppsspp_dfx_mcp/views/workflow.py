"""Workflow view — public JSON contract for the H1 breakpoint-wait tools."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.models.workflow import (
    FrameSnapshotResult,
    TraceAccessResult,
    WaitBreakpointResult,
)
from ppsspp_dfx_mcp.views._base import FrozenModel


def _hex_or_none(value: Any) -> str | None:
    """Format an int-ish address field as a hex string (None passthrough)."""
    if value is None:
        return None
    try:
        return f"0x{int(value):08X}"
    except TypeError, ValueError:
        return str(value)


class WaitBreakpointResponse(FrozenModel):
    """Response view for ppsspp_wait_breakpoint."""

    hit: bool = Field(
        description="True when the CPU entered stepping (breakpoint hit, "
        "or it was already paused when the tool was called).",
    )
    already_paused: bool = Field(
        description="True when the CPU was found in stepping state at arm "
        "time — the hit happened before this tool call.",
    )
    timeout_s: float = Field(
        description="The wait budget that was applied.",
    )
    pc: str | None = Field(
        default=None,
        description="Program counter at the hit, hex string.",
    )
    reason: str | None = Field(
        default=None,
        description="cpu.stepping broadcast reason (e.g. 'breakpoint' / 'memory.breakpoint').",
    )
    related_address: str | None = Field(
        default=None,
        description="Broadcast relatedAddress, hex string (memory "
        "breakpoints: the accessed address).",
    )
    ticks: float | None = Field(
        default=None,
        description="CoreTiming tick count at the hit.",
    )

    @classmethod
    def from_result(cls, result: WaitBreakpointResult, timeout_s: float) -> WaitBreakpointResponse:
        return cls(
            hit=result.hit,
            already_paused=result.already_paused,
            timeout_s=timeout_s,
            pc=_hex_or_none(result.pc),
            reason=result.reason,
            related_address=_hex_or_none(result.related_address),
            ticks=result.ticks,
        )


class TraceAccessResponse(FrozenModel):
    """Response view for ppsspp_trace_memory_access."""

    hit: bool = Field(
        description="True when at least one access was captured.",
    )
    already_paused: bool = Field(
        description="True when the CPU was already paused at arm time — "
        "nothing can hit while paused, so no breakpoint was armed.",
    )
    address: str = Field(
        description="Traced address, hex string.",
    )
    access: str = Field(
        description="Access kind traced: 'read' / 'write' / 'read_write'.",
    )
    timeout_s: float = Field(
        description="The wait budget that was applied.",
    )
    hits: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Captured hits (pc/related_address hex strings; "
        "'registers' / 'backtrace' included when requested).",
    )
    bp_removed: bool = Field(
        default=False,
        description="True when the tool's memory breakpoint is confirmed "
        "gone (list-verified) — always true on normal return.",
    )
    resumed: bool = Field(
        default=False,
        description="True when the tool resumed the CPU it had seen "
        "running at arm time (a hit pauses the CPU; the tool restores "
        "it).",
    )
    note: str | None = Field(
        default=None,
        description="Optional human context (e.g. cleanup caveats).",
    )

    @classmethod
    def from_result(
        cls,
        result: TraceAccessResult,
        *,
        address: int,
        access: str,
        timeout_s: float,
    ) -> TraceAccessResponse:
        hits: list[dict[str, Any]] = []
        for entry in result.hits:
            formatted = dict(entry)
            for key in ("pc", "related_address"):
                if key in formatted:
                    formatted[key] = _hex_or_none(formatted[key])
            hits.append(formatted)
        return cls(
            hit=result.hit,
            already_paused=result.already_paused,
            address=format_address(address),
            access=access,
            timeout_s=timeout_s,
            hits=hits,
            bp_removed=result.bp_removed,
            resumed=result.resumed,
            note=result.note,
        )


class FrameSnapshotResponse(FrozenModel):
    """Response view for ppsspp_frame_snapshot."""

    was_stepping: bool = Field(
        description="True when the CPU was already paused at entry.",
    )
    resumed: bool = Field(
        description="True when the tool resumed a CPU it had paused "
        "(an already-paused CPU is left paused).",
    )
    pc: str = Field(description="Program counter, hex string (high trust).")
    trust_level: str = Field(description="safe_get_pc trust level.")
    registers: dict[str, Any] | None = Field(
        default=None,
        description="Full GPR/FPU/VFPU register dump (when requested).",
    )
    probes: dict[str, Any] | None = Field(
        default=None,
        description="state_observer capture block (when requested).",
    )

    @classmethod
    def from_result(cls, result: FrameSnapshotResult) -> FrameSnapshotResponse:
        return cls(
            was_stepping=result.was_stepping,
            resumed=result.resumed,
            pc=_hex_or_none(result.pc) or "0x00000000",
            trust_level=result.trust_level,
            registers=result.registers,
            probes=result.probes,
        )
