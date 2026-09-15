"""Memory domain models (frozen dataclass)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MemoryReadResult:
    """Result of a memory read.

    Attributes:
        action: Read action performed ('read_bytes' / 'read_u32' /
            'read_string' / 'scan').
        address: Starting address (for scan, the scan start address).
        value: Read value (int for read_u32, str for read_string,
            list[int] for read_bytes, list[dict] for scan — each dict
            has {address: int, context: str} where context is hex-encoded
            pattern bytes).
        size: Number of bytes read (read_bytes), or number of matches
            (scan). Unused for read_u32 / read_string.
    """

    action: str = "read_bytes"
    address: int = 0
    value: Any = None
    size: int = 0
    # True when a read_string hit the byte cap with no NUL
    # terminator — the string was cut short and the caller should know.
    truncated: bool = False


@dataclass(frozen=True)
class MemoryWriteResult:
    """Result of a memory write.

    Attributes:
        address: Target address.
        format: 'u32' or 'bytes'.
        bytes_written: Number of bytes written.
        value: For format='u32', the int value written (None for 'bytes').
            Used by views/memory.py to produce the unified text format
            'Wrote 0xVAL → 0xADDR'.
    """

    address: int = 0
    format: str = "u32"
    bytes_written: int = 0
    value: int | None = None


@dataclass(frozen=True)
class DisassemblyResult:
    """Result of a disassembly.

    Attributes:
        address: Starting address.
        count: Number of instructions disassembled.
        instructions: List of disasm line dicts (text + address).
    """

    address: int = 0
    count: int = 0
    instructions: list[dict[str, Any]] = field(default_factory=list)
