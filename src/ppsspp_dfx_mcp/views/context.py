"""Context view — public JSON contract for ppsspp_context (crash triage pack).

v0.1.7 批 1 后半：把 crash_analysis playbook 的人工三连（查表 + 反汇编 +
回溯）压成单调用。纯客户端编排，零新 WS 事件。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.views._base import FrozenModel


class IdentityView(FrozenModel):
    """Nearest known function at or below the address."""

    name: str = Field(description="Function name from addresses.yaml known_functions.")
    start: str = Field(description="Function start (runtime address, hex).")
    offset: int = Field(description="address - start (bytes into the function).")


class DisasmLineView(FrozenModel):
    """One disassembled instruction."""

    address: str = Field(description="Instruction address (hex), when provided by PPSSPP.")
    text: str = Field(description="Assembly text.")


class ContextResponse(FrozenModel):
    """Response view for ppsspp_context."""

    address: str = Field(description="Queried address (hex).")
    identity: IdentityView | None = Field(
        default=None,
        description="Nearest known function at/below the address; None when unknown.",
    )
    region: str = Field(
        default="",
        description="Memory-map region containing the address (e.g. 'user'), '' when unmapped.",
    )
    disasm: list[DisasmLineView] = Field(
        default_factory=list,
        description="Instructions around the address (window before / at / after).",
    )
    backtrace: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Call stack (paused CPU only; empty when skipped).",
    )
    backtrace_note: str = Field(
        default="",
        description="Why the backtrace is empty (e.g. 'CPU running — pause first').",
    )

    @classmethod
    def build(
        cls,
        address: int,
        identity: IdentityView | None,
        region: str,
        disasm: list[DisasmLineView],
        backtrace: list[dict[str, Any]] | None,
        backtrace_note: str,
    ) -> ContextResponse:
        return cls(
            address=format_address(address),
            identity=identity,
            region=region,
            disasm=disasm,
            backtrace=backtrace or [],
            backtrace_note=backtrace_note,
        )


__all__ = ["ContextResponse", "DisasmLineView", "IdentityView"]
