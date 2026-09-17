"""Assemble tool wrapper.

1 tool exposed:
- ppsspp_assemble(session_id, address, code) — assemble MIPS
  instruction(s) and write the resulting bytes to memory at `address`
  via `memory.assemble`.

DESTRUCTIVE: writes assembled bytes to emulator memory. No undo.

PPSSPP protocol limitation (batch 2 protocol-adapter layer):
PPSSPP's `memory.assemble` only assembles the FIRST instruction when
given semicolon or newline separated input (DisasmSubscriber.cpp:474
schema is singular "instruction"; armips treats ';' as a comment
delimiter). The tool splits multi-instruction input and calls
`memory.assemble` once per instruction, incrementing the address by
the number of bytes written each time.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError, to_tool_error
from ppsspp_dfx_mcp.models.assemble import AssembleResult
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.service.memory_protection import check_protected_address
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.tools._common import require_session_id, translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.assemble import AssembleResponse

AssembleOutput = derive_output_contract("AssembleOutput", AssembleResponse)

logger = logging.getLogger(__name__)

__all__ = ["assemble"]


# Former docstring (kept as comment; description is now the TDQS docstring):
# Assemble MIPS instruction(s) and write to memory.
#
# Writes to protected code-section addresses (kernel memory
# or top.prx code section) are rejected unless ``force=True``. This
# aligns assemble's protection policy with write_memory — previously
# assemble bypassed the check, allowing accidental writes to kernel
# memory or top.prx code section.
#
# Returns:
# AssembleResponse dict: address + code + bytes_written +
# response + text.
#
# Raises:
# ToolError: on session lookup failure, address <= 0, empty code,
# write to protected address without force=True, or WS failure
# (including assembly errors).
@mcp.tool(
    name="ppsspp_assemble",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    ),
)
@translate_tool_errors
async def assemble(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    address: Annotated[
        str,
        Field(
            description=(
                "Target address where assembled bytes will be written, as "
                "a hex string (e.g. '0x08804000'). Must "
                "be a valid MIPS-aligned address for the ISA."
            ),
        ),
    ],
    code: Annotated[
        str,
        Field(
            description=(
                "MIPS assembly source. May be a single instruction "
                "('nop', 'addiu r5, r0, 0x10') or multiple instructions "
                "separated by '\\n' or ';'. The assembler is PPSSPP's "
                "built-in MIPS encoder."
            ),
        ),
    ],
    force: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Set to True to write assembled bytes to "
                "protected code-section addresses (kernel memory < "
                "0x08800000 or top.prx code section 0x08804000-0x08D34000). "
                "Writing to these ranges without force=True raises "
                "ToolError to prevent accidental crashes."
            ),
        ),
    ] = False,
) -> AssembleOutput:
    """PURPOSE: Assemble MIPS instruction(s) and write the resulting bytes to memory.

    USAGE: session_id + address + code ('\n' or ';' separated — PPSSPP assembles one line per call so the tool loops; armips-style ';' comments are NOT supported here).

    BEHAVIOR: DESTRUCTIVE. Protected ranges (kernel, top.prx code) need force=true. A partial write is reported with an error directing you to disassemble and inspect.

    RETURNS: {address, code, bytes_written, response, text}."""
    require_session_id(session_id)
    address_int = parse_address(address)
    if address_int <= 0:
        raise ArgsInvalid("address must be > 0")
    if not code:
        raise ArgsInvalid("code is required")

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_assemble",
            "session_id": session_id,
            "address": address_int,
        },
    )

    # Split multi-instruction input — PPSSPP memory.assemble only
    # assembles the first instruction (DisasmSubscriber.cpp:474 schema is
    # singular "instruction"; armims treats ';' as comment delimiter).
    # Split on '\n' and ';', strip whitespace, drop empty fragments.
    instructions = _split_instructions(code)
    if not instructions:
        raise ArgsInvalid(
            "code contains no valid instructions after splitting on newline/semicolon"
        )

    # N-04: Apply the same protected-address check as write_memory.
    # MIPS I instructions are 4 bytes each, so the full write range is
    # [address, address + len(instructions) * 4). This prevents assemble
    # from bypassing the protection that write_memory enforces.
    check_protected_address(
        address_int,
        byte_count=len(instructions) * 4,
        force=force,
    )

    try:
        async with session_client(session_id) as client:
            # Assemble each instruction sequentially. MIPS I has fixed
            # 4-byte instruction width, so the address increments by 4
            # after each successful assembly. PPSSPP returns
            # {"encoding": <u32>} per call.
            current_addr = address_int
            responses: list[dict[str, Any]] = []
            partial_writes: list[int] = []
            for i, instr in enumerate(instructions):
                try:
                    resp = await client.assemble(address=current_addr, code=instr)
                except Exception as e:
                    # Instruction N failed after instructions 0..N-1
                    # were already written to memory. Report partial
                    # writes so the caller can assess/clean up.
                    # Keep the assembler's own
                    # error text (`from e`, not `from None`) — callers
                    # need the reason to fix the instruction and retry.
                    if partial_writes:
                        raise ArgsInvalid(
                            f"assembly failed at instruction {i + 1}/{len(instructions)} "
                            f"({instr!r}) after {len(partial_writes)} instruction(s) "
                            f"were already written to memory (addresses "
                            f"0x{address_int:08X}–0x{address_int + len(partial_writes) * 4:08X}). "
                            f"Reason: {e}. "
                            f"Use disassemble to inspect partial writes."
                        ) from e
                    raise
                if isinstance(resp, dict):
                    responses.append(resp)
                partial_writes.append(current_addr)
                current_addr += 4
            # Keep the last response for backward compat (view layer
            # reads encoding from it). Aggregate byte count via the
            # number of instructions assembled.
            last_response = responses[-1] if responses else {}
            if responses and "encoding" in last_response:
                last_response = dict(last_response)
                last_response["instruction_count"] = len(instructions)
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e

    result = AssembleResult(
        address=address_int,
        code=code,
        response=last_response,
    )
    return AssembleResponse.from_result(result).model_dump(mode="json")


def _split_instructions(code: str) -> list[str]:
    """Split multi-instruction assembly source into individual instructions.

    Splits on '\n' and ';', strips whitespace, and drops empty fragments.
    Returns a list of non-empty instruction strings.

    PPSSPP's memory.assemble only assembles the first instruction when
    given multi-instruction input (armips treats ';' as comment delimiter;
    the WS schema is singular "instruction"). The tool calls assemble
    once per instruction, incrementing the address by 4 bytes each time.
    """
    # Replace ';' with '\n' then split on '\n' for uniform handling.
    normalized = code.replace(";", "\n")
    return [line.strip() for line in normalized.splitlines() if line.strip()]
