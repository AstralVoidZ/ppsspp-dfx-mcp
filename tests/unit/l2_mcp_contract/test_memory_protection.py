"""L2 contract tests for service.memory_protection.check_protected_address.

Anchors the D-05 / N-04 shared protection policy enforced by both
write_memory and assemble:

- Kernel memory range: 0x00000000 - 0x08800000
- top.prx code section: 0x08804000 - 0x08D34000 (~5.15MB)

The helper is the single source of truth for the protection policy.
Both tools call it before forwarding bytes to PPSSPP — keeping the
policy in one place ensures a future range update propagates to both
tools automatically (DRY).
"""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp.errors import ToolError
from ppsspp_dfx_mcp.service.memory_protection import (
    PROTECTED_RANGES,
    check_protected_address,
)


class TestCheckProtectedAddressRanges:
    """L2: check_protected_address enforces D-05 protected ranges."""

    def test_protected_ranges_constants(self):
        """PROTECTED_RANGES exposes both ranges with stable names."""
        names = {name for name, _ in PROTECTED_RANGES}
        assert names == {"kernel memory", "top.prx code section"}

    def test_kernel_low_address_rejected(self):
        """Address 0x00000000 is in kernel memory → rejected."""
        with pytest.raises(ToolError) as exc_info:
            check_protected_address(0x00000000)
        assert exc_info.value.code == "PROTECTED_ADDRESS"

    def test_kernel_high_boundary_excluded(self):
        """Address 0x08800000 is OUT of kernel range (exclusive) → allowed.

        The kernel range is [0x00000000, 0x08800000) — the upper bound
        is exclusive. 0x08800000 itself falls into the gap between
        kernel memory and the top.prx code section start (0x08804000),
        so it is allowed.
        """
        # No exception raised.
        assert check_protected_address(0x08800000) is None

    def test_top_prx_start_rejected(self):
        """Address 0x08804000 is in top.prx code section → rejected."""
        with pytest.raises(ToolError):
            check_protected_address(0x08804000)

    def test_top_prx_end_boundary_excluded(self):
        """Address 0x08D34000 is OUT of top.prx range (exclusive) → allowed."""
        # No exception raised.
        assert check_protected_address(0x08D34000) is None

    def test_safe_data_address_allowed(self):
        """Address 0x09000000 is in user data → allowed."""
        assert check_protected_address(0x09000000) is None


class TestCheckProtectedAddressByteCount:
    """L2: byte_count extends the check to a range, not just the start."""

    def test_byte_count_zero_uses_single_byte_check(self):
        """byte_count=0 checks only the start address (treated as 1 byte).

        This mirrors write_memory(format='u32') which passes byte_count=0
        because the helper's overlap check with byte_count=0 still
        catches a 4-byte write (start address is enough to flag it).
        """
        with pytest.raises(ToolError):
            check_protected_address(0x08804000, byte_count=0)

    def test_byte_count_extends_range_into_protected(self):
        """Start in safe range but byte_count extends into protected → rejected.

        A write at 0x087FFFFC (safe, just below kernel boundary) with
        byte_count=8 produces range [0x087FFFFC, 0x08800004) which
        crosses the kernel boundary at 0x08800000.
        """
        with pytest.raises(ToolError):
            check_protected_address(0x087FFFFC, byte_count=8)

    def test_byte_count_within_safe_range_allowed(self):
        """byte_count fully within safe range → allowed."""
        # No exception.
        assert check_protected_address(0x09000000, byte_count=64) is None


class TestCheckProtectedAddressForce:
    """L2: force=True bypasses the check entirely."""

    def test_force_bypasses_kernel_address(self):
        """force=True on kernel memory does not raise."""
        assert check_protected_address(0x00000000, force=True) is None

    def test_force_bypasses_code_section(self):
        """force=True on top.prx code section does not raise."""
        assert check_protected_address(0x08804000, byte_count=64, force=True) is None

    def test_force_bypasses_range_straddling_boundary(self):
        """force=True on a range crossing kernel boundary does not raise."""
        assert check_protected_address(0x087FFFFC, byte_count=8, force=True) is None
