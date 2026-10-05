"""Write-register tool wrapper.

1 tool exposed:
- ppsspp_write_register(session_id, name, value) — set a CPU register value
  via `cpu.setReg` under a `with_stepping` context.

DESTRUCTIVE: mutates CPU register state. The DebugClient handles the
with_stepping pause/resume cycle internally.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_value
from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.models.write_register import WriteRegisterResult
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import require_session_id, translate_tool_errors
from ppsspp_dfx_mcp.views.write_register import WriteRegisterResponse

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    WriteRegisterOutput = dict[str, Any]
else:
    WriteRegisterOutput = derive_output_contract("WriteRegisterOutput", WriteRegisterResponse)

logger = logging.getLogger(__name__)

__all__ = ["write_register"]


@mcp.tool(
    name="ppsspp_write_register",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
    ),
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
                "'v0'-'v127'. Numeric aliases like 'r5' are accepted and "
                "normalized (r5 -> a1, i.e. GPR index 5) — prefer the "
                "MIPS standard name. Case-sensitive (lowercase by convention)."
            ),
        ),
    ],
    value: Annotated[
        str,
        Field(
            description=(
                "Value to write, as a hex string (e.g. '0x00000001'). "
                "Treated as an unsigned 32-bit int; values outside "
                "[0, 0xFFFFFFFF] are REJECTED (out-of-32-bit-range error), "
                "not wrapped."
            ),
        ),
    ],
) -> WriteRegisterOutput:
    """PURPOSE: Set a CPU register (GPR/FPU/VFPU names, plus pc/hi/lo).

    USAGE: session_id + name (MIPS ABI names preferred; numeric aliases like 'r5' normalize to the GPR of that index, i.e. r5 -> a1) + value (hex).

    BEHAVIOR: DESTRUCTIVE. Pauses and resumes the CPU automatically (REQUIRED_STEPPING handled internally) — no manual pause needed.

    RETURNS: {name, value, response, text}."""
    require_session_id(session_id)
    if not name:
        raise ArgsInvalid("name is required")

    value_int = parse_value(value)
    # Live-verified: PPSSPP silently wraps negative values
    # (-1 → 0xFFFFFFFF) — the exact behaviour the parameter description
    # promises NOT to do. Enforce the documented fail-fast here.
    if not 0 <= value_int <= 0xFFFFFFFF:
        raise ArgsInvalid(
            f"value {value!r} is out of the 32-bit range [0, 0xFFFFFFFF] "
            f"(and would be silently wrapped by PPSSPP — use the exact "
            f"intended bits)"
        )

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_write_register",
            "session_id": session_id,
            "reg_name": name,
            "value": value_int,
        },
    )

    async with session_client(session_id) as client:
        response = await client.set_reg(name=name, value=value_int)

    result = WriteRegisterResult(
        name=name, value=value_int, response=response if isinstance(response, dict) else None
    )
    return WriteRegisterResponse.from_result(result).model_dump(mode="json")
