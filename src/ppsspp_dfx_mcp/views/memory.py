"""Memory view — public JSON contract for read/write/disassemble tools.

Task 8.3 (unified output format): MemoryReadResponse and MemoryWriteResponse
expose a `text` field alongside the structured fields. `text` follows the
conventions in specs/tdqs-descriptions/spec.md:

- read_u32  → '0x{ADDR:08X}: {VAL} (0x{VAL:X})'
- read_bytes→ '0x{ADDR:08X}: AA BB CC DD ...'  (single-line hex dump)
- read_string→ '0x{ADDR:08X}: {repr(string)}'
- scan      → 'scan: {N} matches at 0x{A1:08X}, 0x{A2:08X}, ...' (first 5)
- write u32 → 'Wrote 0x{VAL:X} → 0x{ADDR:08X}'
- write bytes→ 'Wrote {N} bytes → 0x{ADDR:08X}'

The structured fields (action / address / value / size / format /
bytes_written) remain intact for clients that prefer structured data.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.models.memory import (
    DisassemblyResult,
    MemoryReadResult,
    MemoryWriteResult,
)
from ppsspp_dfx_mcp.views._base import FrozenModel


def _format_read_text(action: str, address: int, value: Any, size: int) -> str:
    """Format the unified text representation of a memory read.

    Args:
        action: 'read_bytes' / 'read_u32' / 'read_string' / 'scan'.
        address: Starting address.
        value: Read value (int / str / list[int] / list[dict]).
        size: Number of bytes (read_bytes only).

    Returns:
        Formatted text string following spec conventions. Empty string
        for scan or unknown actions.
    """
    if action == "read_u32":
        if isinstance(value, int):
            return f"0x{address:08X}: {value} (0x{value:X})"
        return f"0x{address:08X}: {value}"
    if action == "read_bytes":
        if isinstance(value, list):
            hex_bytes = " ".join(f"{b:02X}" for b in value)
            return f"0x{address:08X}: {hex_bytes}"
        return f"0x{address:08X}: {value}"
    if action == "read_string":
        if isinstance(value, str):
            return f"0x{address:08X}: {value!r}"
        return f"0x{address:08X}: {value}"
    if action == "scan":
        # scan: 'scan: N matches at 0x{A1:08X}, 0x{A2:08X}, ...' (first 5)
        if isinstance(value, list):
            count = len(value)
            if count == 0:
                return "scan: 0 matches"
            addrs: list[str] = []
            for m in value[:5]:
                if isinstance(m, dict) and "address" in m:
                    try:
                        addrs.append(f"0x{int(m['address']):08X}")
                    except TypeError, ValueError:
                        addrs.append(str(m["address"]))
            head = ", ".join(addrs)
            suffix = ", ..." if count > 5 else ""
            return f"scan: {count} matches at {head}{suffix}"
        return f"scan: {value}"
    # unknown action: no unified text format defined.
    return ""


def _format_write_text(result: MemoryWriteResult) -> str:
    """Format the unified text representation of a memory write.

    Args:
        result: MemoryWriteResult with address / format / bytes_written /
            value (value populated for format='u32').

    Returns:
        'Wrote 0x{VAL:X} → 0x{ADDR:08X}' for u32, or
        'Wrote {N} bytes → 0x{ADDR:08X}' for bytes.
    """
    if result.format == "u32" and result.value is not None:
        return f"Wrote 0x{result.value:X} → 0x{result.address:08X}"
    return f"Wrote {result.bytes_written} bytes → 0x{result.address:08X}"


class MemoryReadResponse(FrozenModel):
    """Response view for ppsspp_read_memory."""

    action: str = Field(
        description="Read action performed ('read_bytes'/'read_u32'/'read_string'/'scan').",
    )
    address: str = Field(description="Starting address, hex string (e.g. '0x08804000').")
    value: Any = Field(
        default=None,
        description="Read value (int/str/list[int]/list[dict] depending on action).",
    )
    size: int = Field(
        default=0,
        description=(
            "Number of bytes read (read_bytes), or number of matches "
            "(scan). Unused for read_u32 / read_string."
        ),
    )
    text: str = Field(
        default="",
        description=(
            "Unified text representation following spec conventions: "
            "'0xADDR: VAL (0xVAL_HEX)' for read_u32, hex dump for "
            "read_bytes, repr for read_string, "
            "'scan: N matches at 0xA1, 0xA2, ...' for scan."
        ),
    )
    file: str = Field(
        default="",
        description=(
            "Absolute path of the saved raw-byte file when read_bytes "
            "ran with output='file' (hex dump sits beside it as "
            "<file>.hex.txt); empty string otherwise."
        ),
    )

    @classmethod
    def from_result(cls, result: MemoryReadResult) -> MemoryReadResponse:
        text = _format_read_text(
            action=result.action,
            address=result.address,
            value=result.value,
            size=result.size,
        )
        if getattr(result, "truncated", False):
            # Surface truncation in the agent-facing text channel —
            # no new response field, so the JSON contract stays unchanged.
            text += " [TRUNCATED — read cap hit with no NUL terminator]"
        return cls(
            action=result.action,
            address=format_address(result.address),
            value=result.value,
            size=result.size,
            text=text,
        )


class MemoryWriteResponse(FrozenModel):
    """Response view for ppsspp_write_memory."""

    address: str = Field(description="Target address, hex string (e.g. '0x08804000').")
    format: str = Field(description="'u32' or 'bytes'.")
    bytes_written: int = Field(description="Number of bytes written.")
    value: str | None = Field(
        default=None,
        description="For format='u32', the value written as hex string (e.g. '0x00000001'); None for 'bytes'.",
    )
    text: str = Field(
        default="",
        description=(
            "Unified text representation: 'Wrote 0xVAL → 0xADDR' for u32, "
            "'Wrote N bytes → 0xADDR' for bytes."
        ),
    )

    @classmethod
    def from_result(cls, result: MemoryWriteResult) -> MemoryWriteResponse:
        return cls(
            address=format_address(result.address),
            format=result.format,
            bytes_written=result.bytes_written,
            value=format_address(result.value) if result.value is not None else None,
            text=_format_write_text(result),
        )


class DisassemblyResponse(FrozenModel):
    """Response view for ppsspp_disassemble."""

    address: str = Field(description="Starting address, hex string (e.g. '0x08804000').")
    count: int = Field(description="Number of instructions disassembled.")
    instructions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="List of disasm line dicts (text + address).",
    )

    @classmethod
    def from_result(cls, result: DisassemblyResult) -> DisassemblyResponse:
        return cls(
            address=format_address(result.address),
            count=result.count,
            instructions=list(result.instructions),
        )
