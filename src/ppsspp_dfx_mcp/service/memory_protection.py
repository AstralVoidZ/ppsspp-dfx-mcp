"""Memory write protection service.

Shared by write_memory and assemble tools to enforce protected code-section
ranges. Writing to these addresses can crash PPSSPP (JIT cache invalidation
issues) or corrupt game logic. Callers can override with force=True for
intentional patching (e.g., armips-equivalent writes during development).

Protected ranges:
- Kernel memory: 0x00000000 - 0x08800000
- top.prx code section: 0x08804000 - 0x08804000 + 0x530000 (~5.15MB)

Based on project knowledge: top.prx is loaded at 0x08804000 with a size of
~5.15MB (0x530000). Kernel memory (< 0x08800000) is also protected.
"""

from __future__ import annotations

from ppsspp_dfx_mcp.errors import ToolError

# Protected code-section ranges. Writing to these addresses can
# crash PPSSPP (JIT cache invalidation issues) or corrupt game logic.
# Based on project knowledge: top.prx is loaded at 0x08804000 with a
# size of ~5.15MB (0x530000). Kernel memory (< 0x08800000) is also
# protected. Callers can override with force=True for intentional
# patching (e.g., armips-equivalent writes during development).
PROTECTED_RANGE_KERNEL = (0x00000000, 0x08800000)
PROTECTED_RANGE_TOP_PRX = (0x08804000, 0x08804000 + 0x530000)

# All protected ranges, in order of precedence for error messages.
PROTECTED_RANGES: tuple[tuple[str, tuple[int, int]], ...] = (
    ("kernel memory", PROTECTED_RANGE_KERNEL),
    ("top.prx code section", PROTECTED_RANGE_TOP_PRX),
)


def check_protected_address(
    address: int, *, byte_count: int = 0, force: bool = False
) -> None:
    """Raise ToolError if the address range overlaps a protected code-section range.

    Protected ranges:
    - Kernel memory: 0x00000000 - 0x08800000
    - top.prx code section: 0x08804000 - 0x08D34000 (~5.15MB)

    For byte_count > 0, checks the full range [address, address + byte_count)
    for overlap with protected ranges. For byte_count == 0, only the start
    address is checked (treated as a 1-byte write).

    Args:
        address: Start address of the write.
        byte_count: Number of bytes to write (0 = single-byte / fixed-size check).
        force: If True, skip the protection check (caller accepts the risk).

    Raises:
        ToolError: if the range overlaps a protected range and force is False.
            Error code is "PROTECTED_ADDRESS". The message includes the
            offending range, the protected range name+bounds, and a hint to
            set force=True to override.
    """
    if force:
        return

    end = address + byte_count if byte_count > 0 else address + 1
    for name, (lo, hi) in PROTECTED_RANGES:
        # Overlap check: [address, end) ∩ [lo, hi) ≠ ∅
        if address < hi and end > lo:
            raise ToolError(
                f"address range 0x{address:08X}-0x{end:08X} overlaps protected "
                f"{name} (0x{lo:08X}-0x{hi:08X}). Writing to this range can "
                f"crash PPSSPP (JIT cache invalidation). "
                f"Set force=True to override.",
                code="PROTECTED_ADDRESS",
            )
