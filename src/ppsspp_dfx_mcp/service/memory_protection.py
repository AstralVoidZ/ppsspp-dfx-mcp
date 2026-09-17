"""Memory write protection service.

Shared by write_memory and assemble tools to enforce protected code-section
ranges. Writing to these addresses can crash PPSSPP (JIT cache invalidation
issues) or corrupt game logic. Callers can override with force=True for
intentional patching (e.g., armips-equivalent writes during development).

Protected ranges:
- Kernel memory: 0x00000000 - 0x08800000 (PSP-generic: user-space
  partitions start at 0x08800000)
- top.prx code section: base from addresses.yaml `top_base.ppsspp`
  (falling back to this project's default 0x08804000) + the heuristic
  0x530000 section size
"""

from __future__ import annotations

from ppsspp_dfx_mcp import config
from ppsspp_dfx_mcp.errors import ToolError

PROTECTED_RANGE_KERNEL = (0x00000000, 0x08800000)
DEFAULT_TOP_PRX_BASE = 0x08804000
DEFAULT_TOP_PRX_SIZE = 0x530000

# Fallback policy when addresses.yaml carries no usable `top_base.ppsspp`.
PROTECTED_RANGES: tuple[tuple[str, tuple[int, int]], ...] = (
    ("kernel memory", PROTECTED_RANGE_KERNEL),
    ("top.prx code section", (DEFAULT_TOP_PRX_BASE, DEFAULT_TOP_PRX_BASE + DEFAULT_TOP_PRX_SIZE)),
)


def _effective_ranges() -> tuple[tuple[str, tuple[int, int]], ...]:
    """PROTECTED_RANGES with the per-game top.prx base from addresses.yaml.

    The kernel range is PSP-generic and stays constant; the top.prx base
    is project knowledge and MUST come from the single address
    source of truth (`top_base.ppsspp`), not from a compiled-in literal.
    """
    top = config.addresses().get("top_base")
    base = top.get("ppsspp") if isinstance(top, dict) else None
    if not isinstance(base, int) or isinstance(base, bool) or base <= 0:
        return PROTECTED_RANGES
    return (
        ("kernel memory", PROTECTED_RANGE_KERNEL),
        ("top.prx code section", (base, base + DEFAULT_TOP_PRX_SIZE)),
    )


def check_protected_address(address: int, *, byte_count: int = 0, force: bool = False) -> None:
    """Raise ToolError if the address range overlaps a protected range.

    The top.prx section base follows addresses.yaml `top_base.ppsspp`
    (fallback: this project's default 0x08804000); kernel memory is
    PSP-generic.

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
    for name, (lo, hi) in _effective_ranges():
        # Overlap check: [address, end) ∩ [lo, hi) ≠ ∅
        if address < hi and end > lo:
            raise ToolError(
                f"address range 0x{address:08X}-0x{end:08X} overlaps protected "
                f"{name} (0x{lo:08X}-0x{hi:08X}). Writing to this range can "
                f"crash PPSSPP (JIT cache invalidation). "
                f"Set force=True to override.",
                code="PROTECTED_ADDRESS",
            )
