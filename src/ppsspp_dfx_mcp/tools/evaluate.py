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
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError, to_tool_error
from ppsspp_dfx_mcp.models.evaluate import EvaluateResult
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.tools._common import require_session_id, translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.evaluate import EvaluateResponse

EvaluateOutput = derive_output_contract("EvaluateOutput", EvaluateResponse)

logger = logging.getLogger(__name__)

__all__ = ["evaluate"]


def _extract_value(response: dict[str, Any] | None) -> int | None:
    """Best-effort numeric extraction from a cpu.evaluate response.

    PPSSPP's response shape varies; tolerate 'value' / 'result' / 'int' /
    'uintValue' keys, each as int or numeric str. Return None if no
    numeric value is found.
    """
    if not isinstance(response, dict):
        return None
    for key in ("value", "result", "int", "uintValue"):
        raw = response.get(key)
        if isinstance(raw, bool):
            # bool is a subclass of int; skip to avoid surprises.
            continue
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            try:
                return int(raw, 0) if raw.startswith(("0x", "0X")) else int(raw)
            except ValueError:
                continue
    return None


# Former docstring (kept as comment; description is now the TDQS docstring):
# Evaluate a debugger expression.
#
# Returns:
# EvaluateResponse dict: expression + value + response + text.
#
# Raises:
# ToolError: on session lookup failure, empty expression, or WS
# failure.
@mcp.tool(
    name="ppsspp_evaluate",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
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

    try:
        async with session_client(session_id) as client:
            response = await client.evaluate(expression=expression)
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e

    result = EvaluateResult(
        expression=expression,
        value=_extract_value(response if isinstance(response, dict) else None),
        response=response if isinstance(response, dict) else None,
    )
    return EvaluateResponse.from_result(result).model_dump(mode="json")
