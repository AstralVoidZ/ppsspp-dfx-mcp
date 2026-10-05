"""Evaluate tool wrapper.

1 tool exposed:
- ppsspp_evaluate(session_id, expression) — evaluate a debugger
  expression (e.g., 'r5 + 0x10') via `cpu.evaluate` under a
  `with_stepping` context.

READ-ONLY: the with_stepping pause/resume cycle is handled internally
by the DebugClient; only the expression result is returned.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.core.value_expr import extract_value as _extract_value
from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.models.evaluate import EvaluateResult
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import require_session_id, translate_tool_errors
from ppsspp_dfx_mcp.views.evaluate import EvaluateResponse

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    EvaluateOutput = dict[str, Any]
else:
    EvaluateOutput = derive_output_contract("EvaluateOutput", EvaluateResponse)

logger = logging.getLogger(__name__)

__all__ = ["evaluate"]

# `_extract_value` is imported from `core/value_expr.py` (where the algorithm
# now lives, W19) and re-exported here for historical callers/tests.


@mcp.tool(
    name="ppsspp_evaluate",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
@translate_tool_errors
async def evaluate(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    expression: Annotated[
        str,
        Field(
            description=(
                "Debugger expression to evaluate. Examples: 'r5 + 0x10', "
                "'pc', 'r5 + r6'. The supported syntax is whatever "
                "PPSSPP's expression evaluator accepts. "
                "Note: PPSSPP's evaluator does NOT support dereference "
                "syntax like '*0x08804000' — use read_u32 / read_bytes "
                "instead to read memory at an address."
            ),
        ),
    ],
) -> EvaluateOutput:
    """PURPOSE: Evaluate a debugger expression (register names, hex literals, simple arithmetic).

    USAGE: session_id + expression. No '*addr' dereference syntax — read memory with read_u32 instead.

    BEHAVIOR: READ-ONLY. Pauses/resumes the CPU internally.

    RETURNS: {expression, value, response, text}."""
    require_session_id(session_id)
    if not expression:
        raise ArgsInvalid("expression is required")

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_evaluate",
            "session_id": session_id,
            "expression": expression,
        },
    )

    async with session_client(session_id) as client:
        response = await client.evaluate(expression=expression)

    result = EvaluateResult(
        expression=expression,
        value=_extract_value(response if isinstance(response, dict) else None),
        response=response if isinstance(response, dict) else None,
    )
    return EvaluateResponse.from_result(result).model_dump(mode="json")
