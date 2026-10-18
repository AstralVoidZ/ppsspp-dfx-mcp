"""L1 tool-layer locks for the write-protection paths (review C-1 / S-1).

Why this file exists
--------------------
`CHANGELOG.md` (v0.1.7, write-protection batch) claimed a file with exactly
this name and "8 tool-layer locks". It was never committed: `git log --all`
and every tag's `ls-tree` are empty for the name, and no test referenced the
new paths (`resolve_session_modules`, `check_protected_address(..., modules=)`)
at all. The protection stayed bypassable as a direct consequence.

The lock that matters is `test_overlap_only_write_into_kernel_is_rejected`:
before C-1 the guard exempted a write **wholesale** as soon as any byte of it
touched an exempt span, so `0x0000FFF0 + 64 KiB` (16 bytes of scratchpad,
the rest kernel RAM) was allowed without `force=True` and without the
FORCE OVERRIDE audit record.

Every assertion below is falsifiable: revert the corresponding production
line and exactly one of these fails.
"""

from __future__ import annotations

from typing import Any

import pytest

from ppsspp_dfx_mcp.service.memory_protection import (
    ProtectedAddress,
    _declared_data_ranges,
    _exempt_segments,
    _subtract_spans,
    check_protected_address,
    conservative_code_extent,
    narrow_code_extent,
    resolve_session_modules,
)

# Exempt spans the guard knows about (see memory_protection constants).
SCRATCHPAD = (0x00010000, 0x00014000)
VRAM = (0x04000000, 0x04800000)


class TestExemptSpanIsNotWholesale:
    """C-1: overlapping an exempt span must not exempt the rest of the write."""

    def test_overlap_only_write_into_kernel_is_rejected(self) -> None:
        """THE C-1 REGRESSION: 16 bytes of scratchpad must not free 64 KiB of kernel."""
        assert _exempt_segments(0x0000FFF0, 0x0001FFF0), "probe must overlap scratchpad"
        with pytest.raises(ProtectedAddress) as exc_info:
            check_protected_address(0x0000FFF0, byte_count=0x10000)
        # The message names the offending segment, not the whole (legal+illegal) range.
        msg = str(exc_info.value)
        assert "segment" in msg and "kernel memory" in msg

    def test_fully_exempt_spans_are_allowed(self) -> None:
        """A write wholly inside a declared span stays allowed (no over-blocking)."""
        for lo, hi in (SCRATCHPAD, VRAM):
            assert check_protected_address(lo, byte_count=hi - lo) is None
        # Single-byte write at the span's first byte is still fine.
        assert check_protected_address(SCRATCHPAD[0]) is None

    def test_write_exiting_an_exempt_span_is_rejected(self) -> None:
        """Leaving the scratchpad into kernel RAM at the very next byte is rejected."""
        with pytest.raises(ProtectedAddress):
            check_protected_address(SCRATCHPAD[1] - 0x10, byte_count=0x11)


class TestDeclaredDataExemption:
    """Declared data variables inside the code image remain writable."""

    @staticmethod
    def _pin_declared(monkeypatch: pytest.MonkeyPatch, span: tuple[int, int]) -> None:
        lo, hi = span
        monkeypatch.setattr(
            "ppsspp_dfx_mcp.service.memory_protection._declared_data_ranges",
            lambda: ((lo, hi, "test fixture"),),
        )

    def test_declared_span_inside_code_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        start, _end = conservative_code_extent()
        span = (start + 0x1000, start + 0x1004)
        self._pin_declared(monkeypatch, span)
        assert check_protected_address(span[0], byte_count=4) is None

    def test_declared_span_straddle_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A write that only brushes the declared 4 bytes is still checked."""
        start, _end = conservative_code_extent()
        span = (start + 0x1000, start + 0x1004)
        self._pin_declared(monkeypatch, span)
        with pytest.raises(ProtectedAddress):
            check_protected_address(span[0] - 4, byte_count=8)


class TestExtentSelection:
    """`modules=` is authoritative; an unknown extent fails closed."""

    def test_session_module_extent_is_authoritative(self) -> None:
        base = 0x08804000
        modules: list[dict[str, Any]] = [{"name": "top.prx", "address": base, "size": 0x1000}]
        inside_small = base + 0x800
        outside_small_inside_wide = conservative_code_extent()[0] + 0x100000
        assert conservative_code_extent()[0] <= outside_small_inside_wide < conservative_code_extent()[1]

        with pytest.raises(ProtectedAddress):
            check_protected_address(inside_small, byte_count=4, modules=modules)
        # With the real (small) extent this address is ordinary data; the
        # no-modules fallback would reject it.
        assert check_protected_address(outside_small_inside_wide, byte_count=4, modules=modules) is None
        with pytest.raises(ProtectedAddress):
            check_protected_address(outside_small_inside_wide, byte_count=4)

    def test_unknown_extent_fails_closed(self) -> None:
        wide_lo, wide_hi = conservative_code_extent()
        narrow_lo, narrow_hi = narrow_code_extent()
        assert narrow_hi <= wide_hi
        with pytest.raises(ProtectedAddress):
            check_protected_address(wide_lo, byte_count=4, modules=None)
        # "base" is the narrow pre-flight extent: just past it is allowed.
        assert check_protected_address(narrow_hi, byte_count=4, modules=None, extent_when_unknown="base") is None
        with pytest.raises(ProtectedAddress):
            check_protected_address(narrow_lo, byte_count=4, modules=None, extent_when_unknown="base")


class TestHelperExactness:
    """The span arithmetic itself, independent of policy."""

    def test_subtract_spans_is_exact(self) -> None:
        assert _subtract_spans([(0, 100)], 40, 60) == [(0, 40), (60, 100)]
        assert _subtract_spans([(0, 100)], 0, 100) == []
        assert _subtract_spans([(0, 100)], 100, 200) == [(0, 100)]
        assert _subtract_spans([(0, 100), (200, 300)], 50, 250) == [(0, 50), (250, 300)]


class TestSessionModuleResolution:
    """`resolve_session_modules` must degrade to None, never to a silent pass."""

    async def test_unusable_module_list_returns_none(self) -> None:
        class Raising:
            async def module_list(self) -> Any:
                raise RuntimeError("transport down")

        class Garbage:
            async def module_list(self) -> Any:
                return {"not": "a list"}

        assert await resolve_session_modules(Raising(), "s1") is None
        assert await resolve_session_modules(Garbage(), "s1") is None

    async def test_usable_module_list_is_returned(self) -> None:
        """The wire shape is a mapping with a `modules` list, not a bare list."""
        payload = [{"name": "top.prx", "address": 0x08804000, "size": 0x530000}]

        class Fine:
            async def module_list(self) -> Any:
                return {"modules": payload}

        assert await resolve_session_modules(Fine(), "s1") == payload
