"""Write-register tool wrapper.

1 tool exposed:
- ppsspp_write_register(session_id, name, value) — set a CPU register value
  via `cpu.setReg` under a `with_stepping` context.

DESTRUCTIVE: mutates CPU register state. The DebugClient handles the
with_stepping pause/resume cycle internally.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from pydantic import Field
from mcp.types import ToolAnnotations

from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.tools._common import require_session_id
from ppsspp_dfx_mcp.address import parse_value
from ppsspp_dfx_mcp.errors import ToolError, to_tool_error
from ppsspp_dfx_mcp.models.write_register import WriteRegisterResult
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.views.write_register import WriteRegisterResponse
from ppsspp_dfx_mcp.server import mcp

from ppsspp_dfx_mcp.views._contract import derive_output_contract

WriteRegisterOutput = derive_output_contract("WriteRegisterOutput", WriteRegisterResponse)

logger = logging.getLogger(__name__)

__all__ = ["write_register"]


# Former docstring (kept as comment; description is now the TDQS docstring):
# Write a value to a CPU register.
#
# Returns:
# WriteRegisterResponse dict: name + value + response + text.
#
# Raises:
# ToolError: on session lookup failure, empty name, or WS failure.
@mcp.tool(
    name="ppsspp_write_register",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False),
)
@translate_tool_errors
async def write_register(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    name: Annotated[
        str,
        Field(
            description=(
                "CPU register name (MIPS standard names). GPRs: "
                "'v0'/'v1'/'a0'-'a3'/'t0'-'t9'/'s0'-'s7'/'gp'/'sp'/'fp'/"
                "'ra'/'hi'/'lo'/'pc'. FPU: 'f0'-'f31'. VFPU: "
                "'v0'-'v127'. Numeric aliases like 'r5' are NOT accepted "
                "by PPSSPP — use the MIPS standard name (e.g., 'a1' "
                "instead of 'r5'). Case-sensitive (lowercase by convention)."
            ),
        ),
    ],
    value: Annotated[
        str,
        Field(
            description=(
                "Value to write, as a hex string (e.g. '0x00000001'). "
                "Treated as an unsigned 32-bit int; values outside "
                "[0, 0xFFFFFFFF] are wrapped by PPSSPP."
            ),
        ),
    ],
) -> WriteRegisterOutput:
    """PURPOSE: Set a CPU register (GPR/FPU/VFPU names, plus pc/hi/lo).
    
    USAGE: session_id + name (MIPS ABI names only — 'r5' normalizes to 'v1') + value (hex).
    
    BEHAVIOR: DESTRUCTIVE. Pauses and resumes the CPU automatically (REQUIRED_STEPPING handled internally) — no manual pause needed.
    
    RETURNS: {name, value, response, text}."""
    require_session_id(session_id)
    if not name:
        raise ToolError("name is required", code="INTERNAL")

    value_int = parse_value(value)

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_write_register",
            "session_id": session_id,
            "reg_name": name,
            "value": value_int,
        },
    )

    try:
        async with session_client(session_id) as client:
            response = await client.set_reg(name=name, value=value_int)
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e

    result = WriteRegisterResult(
        name=name, value=value_int, response=response if isinstance(response, dict) else None
    )
    return WriteRegisterResponse.from_result(result).model_dump(mode="json")
