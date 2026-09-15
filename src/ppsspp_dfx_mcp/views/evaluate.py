"""Evaluate view — public JSON contract for ppsspp_evaluate.

Task 8.3 (unified output format): exposes a `text` field alongside the
structured fields, following the convention:
    '{EXPR} = 0x{VAL:08X}'
(or '{EXPR} = {VAL}' if value is None).
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.models.evaluate import EvaluateResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class EvaluateResponse(FrozenModel):
    """Response view for ppsspp_evaluate."""

    expression: str = Field(description="The expression evaluated.")
    value: int | None = Field(
        default=None,
        description="Evaluated value (int) if numeric, else None.",
    )
    response: dict[str, Any] | None = Field(
        default=None,
        description="Raw PPSSPP WebSocket response.",
    )
    text: str = Field(
        default="",
        description=(
            "Unified text representation: '{EXPR} = 0x{VAL:X}' when value "
            "is an int, or '{EXPR} = {value!r}' otherwise."
        ),
    )

    @classmethod
    def from_result(cls, result: EvaluateResult) -> "EvaluateResponse":
        if isinstance(result.value, int):
            text = f"{result.expression} = 0x{result.value:08X}"
        else:
            text = f"{result.expression} = {result.value!r}"
        return cls(
            expression=result.expression,
            value=result.value,
            response=result.response,
            text=text,
        )
