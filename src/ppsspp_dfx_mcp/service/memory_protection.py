"""Memory write protection service.

Shared by write_memory and assemble tools to enforce protected code-section
ranges. Writing to these addresses can crash PPSSPP (JIT cache invalidation
issues) or corrupt game logic. Callers can override with force=True for
intentional patching (e.g., armips-equivalent writes during development).

Measured 2026-09-30 on the real TOPX image, two defects were fixed here:

1. The "kernel memory" range ran 0x00000000-0x08800000, which also covers
   VRAM (0x04000000) and scratchpad (0x00010000). Both are legitimate
   write targets — the error text calling them "kernel memory" was simply
   wrong. Only the real kernel region is protected now.

2. The top.prx extent used a compiled-in heuristic size (0x530000). On the
   real image the module ended at 0x08B4BD00 (size 3,439,872) while the
   guard's upper bound sat at 0x08D34000 — 1.95 MB of ordinary user data
   was rejected as "code", including the project's own `game_mode`
   variable. The extent now comes from the running session's module list.

Two-pass posture. The guard runs twice, and the two
passes deliberately use DIFFERENT extents when no module list is known:

* ``check_protected_address_static`` runs before the session exists, so it
  cannot ask the debugger anything. It covers only the module base plus the
  safety margin; its job is to reject the obviously-wrong write with zero
  side effects without inventing an extent that would block ordinary data
  inside the module image.
* ``check_protected_address`` runs inside the session and is the
  AUTHORITATIVE check. Given the session's module list it uses the real
  extent, which is what keeps ordinary data inside the module image
  writable. Without a usable list it fails CLOSED by falling
  back to the conservative heuristic extent, so a missing module list can never
  narrow the guard.

The earlier wording ("fails CLOSED for the game-module base") described a
guard that silently stopped protecting everything past base+64 KiB as soon
as the module list was unavailable — on the measured TOPX image that left
about 3.2 MB, ~97% of the code section, writable without force. Failing
closed means the WIDEST defensible extent, not the narrowest.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Literal

from ppsspp_dfx_mcp import config
from ppsspp_dfx_mcp.errors import ProtectedAddress

logger = logging.getLogger(__name__)

# PSP-generic partitions. User space starts at 0x08800000, so kernel memory
# is [0x00000000, 0x08800000). The scratchpad (0x00010000) and VRAM
# (0x04000000) live inside that span but are ordinary memory, not kernel —
# protecting them blocked legitimate work, so they are excluded below.
PROTECTED_RANGE_KERNEL = (0x00000000, 0x08800000)

# Legitimate memory that happens to sit inside the kernel span.
_ALLOWED_IN_KERNEL_SPAN: tuple[tuple[int, int, str], ...] = (
    (0x00010000, 0x00014000, "Scratchpad"),
    (0x04000000, 0x04800000, "VRAM"),
)

DEFAULT_TOP_PRX_BASE = 0x08804000

# Head-room added after a module's reported end to cover alignment padding
# and trailing metadata. 64 KiB is deliberately far below the 1.95 MB the
# old heuristic over-reached by.
SAFETY_MARGIN_BYTES = 64 * 1024

# Upper bound for the game module used ONLY when the running session cannot
# report its module list. This is the conservative compiled-in heuristic and it
# over-approximates every image measured so far (the real TOPX top.prx ends
# at 0x08B4BD00, 0x348000 bytes, well inside it) — which is what a
# fail-closed fallback needs. It is deliberately NOT used by the no-I/O
# pre-flight, which must not reject ordinary data inside
# the module image before a session exists.
HEURISTIC_TOP_PRX_SIZE = 0x530000

# Only the game module is code. PPSSPP also reports HLE libraries
# (sceMpeg_library, mp4msv_module, ...) whose data must stay writable, so
# the boundary is taken from `top_base` and matched by name/extent rather
# than blanket-protecting every reported module.
ModuleRange = tuple[str, int, int]


def _to_int(value: Any) -> int | None:
    """Parse a hex string / int address, rejecting bools and junk."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


# Range names are part of the error contract (callers branch on them), so
# they are named here once instead of being repeated as literals.
KERNEL_RANGE_NAME = "kernel memory"
CODE_SECTION_RANGE_NAME = "top.prx code section"

# Backward-compatible view (imported by tests/unit/l2_mcp_contract/
# test_memory_protection.py). The authoritative runtime-aware ranges come
# from effective_ranges(); this tuple mirrors the no-module-information
# fallback, i.e. the fail-closed heuristic extent.
PROTECTED_RANGES: tuple[tuple[str, tuple[int, int]], ...] = (
    (KERNEL_RANGE_NAME, PROTECTED_RANGE_KERNEL),
    (
        CODE_SECTION_RANGE_NAME,
        (DEFAULT_TOP_PRX_BASE, DEFAULT_TOP_PRX_BASE + HEURISTIC_TOP_PRX_SIZE),
    ),
)


def _configured_base() -> int | None:
    """top.prx base from addresses.yaml — the single source of truth."""
    try:
        top = config.addresses().get("top_base")
    except Exception as exc:  # noqa: BLE001 - config problems must not crash writes
        logger.warning("protection: cannot read top_base: %s", exc)
        return None
    base = top.get("ppsspp") if isinstance(top, dict) else None
    return base if isinstance(base, int) and not isinstance(base, bool) and base > 0 else None


def code_section_range(
    modules: Sequence[dict[str, Any]] | None,
) -> tuple[int, int] | None:
    """The game module's code extent, from the running session.

    Returns None when nothing usable is known; the caller then fails
    closed rather than guessing an extent.
    """
    base = _configured_base() or DEFAULT_TOP_PRX_BASE
    if not modules:
        return None

    best: ModuleRange | None = None
    for entry in modules:
        if not isinstance(entry, dict):
            continue
        addr = _to_int(entry.get("address"))
        size = _to_int(entry.get("size"))
        if addr is None or size is None or size <= 0:
            # Junk entry: unusable, but must not be fatal.
            continue
        name = str(entry.get("name") or "")
        if addr != base and "top" not in name.lower():
            # Not the game module (HLE libraries report other addresses).
            continue
        if best is None or size > best[2] - best[1]:
            best = (name, addr, addr + size)

    if best is None:
        return None
    _name, start, end = best
    return start, end + SAFETY_MARGIN_BYTES


def _configured_module_base() -> int:
    """The game module's load base, from config or the built-in default."""
    return _configured_base() or DEFAULT_TOP_PRX_BASE


def narrow_code_extent() -> tuple[int, int]:
    """The region that is certainly code without consulting a session.

    Module base + safety margin. Only the no-I/O pre-flight uses this: it is
    deliberately narrower than the heuristic extent, because a pass that
    cannot see the module image must not reject ordinary data inside it
    (rejecting ordinary data inside the module image is the very failure
    this module exists to prevent).
    """
    base = _configured_module_base()
    return base, base + SAFETY_MARGIN_BYTES


def conservative_code_extent() -> tuple[int, int]:
    """Fail-closed extent used when the session's module list is unknown.

    Over-approximates the whole module image. Callers that legitimately
    write inside it either declare the address (see `_declared_data_ranges`)
    or pass force=True.
    """
    base = _configured_module_base()
    return base, base + HEURISTIC_TOP_PRX_SIZE


def _declared_data_ranges() -> tuple[tuple[int, int, str], ...]:
    """Data addresses the project declares inside the module image.

    Narrowing the code section is NOT sufficient on its own: on
    the real TOPX image `game_mode` (0x08A0D000) sits 2.13 MB INTO the
    loaded top.prx, well within its real extent. It is a data variable
    that happens to be allocated alongside code, so it needs an explicit
    exemption sourced from addresses.yaml — not a wider code section.

    Only addresses the project already declares are honoured; nothing is
    hard-coded here (constitution principle I, single source of truth).
    """
    try:
        addrs = config.addresses()
    except Exception as exc:  # noqa: BLE001 - config issues must not block writes
        logger.warning("protection: cannot read declared data addrs: %s", exc)
        return ()

    out: list[tuple[int, int, str]] = []
    probes = addrs.get("state_probes")
    if isinstance(probes, dict):
        for name, spec in probes.items():
            if not isinstance(spec, dict):
                continue
            addr = _to_int(spec.get("address"))
            if addr is None:
                continue
            size = _to_int(spec.get("size")) or 4
            out.append((addr, addr + max(size, 1), f"declared probe {name}"))
    # Standalone `*_addr` scalars, e.g. game_mode_addr.
    for key, value in addrs.items():
        if not key.endswith("_addr") or isinstance(value, (dict, list)):
            continue
        addr = _to_int(value)
        if addr is not None:
            out.append((addr, addr + 4, f"declared {key}"))
    return tuple(out)


def _in_allowed_span(address: int, end: int) -> str | None:
    """Name the legitimate region covering [address, end), if any."""
    for lo, hi, label in _ALLOWED_IN_KERNEL_SPAN:
        if address < hi and end > lo:
            return label
    for lo, hi, label in _declared_data_ranges():
        if address < hi and end > lo:
            return label
    return None


def effective_ranges(
    modules: Sequence[dict[str, Any]] | None = None,
    *,
    extent_when_unknown: Literal["wide", "base"] = "wide",
) -> tuple[tuple[str, tuple[int, int]], ...]:
    """Protected ranges for the authoritative in-session check.

    `modules` is the calling session's module list. When it yields no
    usable extent the guard falls back according to `extent_when_unknown`
    — "wide" (the default) fails closed for an in-session caller, "base" is
    the narrow pre-flight extent.
    """
    code = _code_extent(modules, extent_when_unknown)
    return (
        (KERNEL_RANGE_NAME, PROTECTED_RANGE_KERNEL),
        (CODE_SECTION_RANGE_NAME, code),
    )


def _code_extent(
    modules: Sequence[dict[str, Any]] | None,
    extent_when_unknown: str,
) -> tuple[int, int]:
    """The code extent for this check, or the requested fallback."""
    code = code_section_range(modules)
    if code is not None:
        return code
    if extent_when_unknown == "base":
        return narrow_code_extent()
    if extent_when_unknown == "wide":
        return conservative_code_extent()
    raise ValueError(f"extent_when_unknown must be 'wide' or 'base', got {extent_when_unknown!r}")


def check_protected_address_static(
    address: int,
    *,
    byte_count: int = 0,
    force: bool = False,
) -> None:
    """No-I/O guard used before any transport call.

    The write guard must reject a protected address with
    ZERO side effects, so this pass cannot ask the debugger for a module
    list. It therefore uses the narrow config-derived extent (module base +
    safety margin), which catches a write into the code section proper
    without blocking ordinary data further inside an image it cannot see
    (blocking that would re-introduce the over-reaching extent described
    in the module docstring).

    This pass is NOT authoritative — it cannot see the real module extent.
    Callers must follow it with ``check_protected_address`` once a session
    is open.
    """
    check_protected_address(
        address,
        byte_count=byte_count,
        force=force,
        modules=None,
        extent_when_unknown="base",
    )


def check_protected_address(
    address: int,
    *,
    byte_count: int = 0,
    force: bool = False,
    modules: Sequence[dict[str, Any]] | None = None,
    extent_when_unknown: Literal["wide", "base"] = "wide",
) -> None:
    """Raise ProtectedAddress if the write overlaps a protected range.

    `modules` is the calling SESSION's module list. Passing another
    session's list would answer the wrong question — the memory layout
    being written is the one this session loaded.

    Args:
        address: Start address of the write.
        byte_count: Number of bytes to write (0 = single-byte check).
        force: If True, skip the check (caller accepts the risk).
        modules: This session's loaded-module list, or None if unknown.
        extent_when_unknown: Module extent to assume when `modules` yields
            no usable extent. "wide" (default) is the fail-closed heuristic
            and is what every in-session caller wants; "base" is the narrow
            base+margin extent for the no-I/O pre-flight.

    Raises:
        ProtectedAddress: on overlap with force=False.
        ValueError: on an unknown `extent_when_unknown` value.
    """
    if force:
        # A protected-range overwrite is the highest-risk
        # operation this server performs — it must leave a trace. The trace
        # must also be ATTRIBUTABLE: audit="force_override" plus the current
        # request id (the middleware ContextVar) so a forced write can be
        # tied to the request that issued it.
        from ppsspp_dfx_mcp.middleware import get_request_id

        logger.warning(
            "PROTECTED-WRITE FORCE OVERRIDE: 0x%08X..0x%08X (%d bytes) — "
            "JIT cache invalidation may crash PPSSPP",
            address,
            address + max(byte_count, 1),
            max(byte_count, 1),
            extra={"audit": "force_override", "request_id": get_request_id()},
        )
        return

    end = address + byte_count if byte_count > 0 else address + 1

    allowed = _in_allowed_span(address, end)
    for name, (lo, hi) in effective_ranges(modules, extent_when_unknown=extent_when_unknown):
        if not (address < hi and end > lo):
            continue
        if allowed is not None:
            # Legitimate memory inside a protected span:
            #  - scratchpad / VRAM sit inside the kernel span;
            #  - declared data variables (e.g. game_mode) sit inside the
            #    module image alongside code.
            # Both are project-declared, legitimate write targets.
            logger.debug(
                "protection: allowing 0x%08X-0x%08X inside %s (%s)",
                address,
                end,
                name,
                allowed,
            )
            continue
        hint = ""
        if name == CODE_SECTION_RANGE_NAME and code_section_range(modules) is None:
            # Explain WHY the extent looks wide instead of leaving the
            # caller to guess, and name both legitimate escapes.
            hint = (
                " This is the fail-closed fallback extent because the"
                " session's module list was unavailable; pass force=True to"
                " override, or declare the address in addresses.yaml."
            )
        raise ProtectedAddress(
            f"address range 0x{address:08X}-0x{end:08X} overlaps protected "
            f"{name} (0x{lo:08X}-0x{hi:08X}). Writing to this range can "
            f"crash PPSSPP (JIT cache invalidation). "
            f"Set force=True to override.{hint}"
        )


async def resolve_session_modules(
    client: Any,
    session_id: str,
) -> list[dict[str, Any]] | None:
    """This session's module list, for the authoritative protection check.

    Returns None when the list cannot be obtained or is unusable, and logs
    why: the caller then runs the check with the conservative extent, so an
    unavailable module list downgrades to "reject unless force=True"
    instead of silently skipping the guard.

    The list belongs to ONE session — module layouts differ per session, so
    answering with another session's layout would check the wrong memory.
    """
    try:
        payload = await client.module_list()
    except Exception as exc:  # noqa: BLE001 - must never block the write path
        logger.warning(
            "protection: module list unavailable for session %s (%s) — using the "
            "conservative code extent; writes inside the module image need "
            "force=True or a declared address",
            session_id,
            exc,
        )
        return None
    if not isinstance(payload, dict):
        logger.warning(
            "protection: module list for session %s had type %s, not a mapping — "
            "using the conservative code extent",
            session_id,
            type(payload).__name__,
        )
        return None
    modules = [m for m in payload.get("modules", []) if isinstance(m, dict)]
    if not modules:
        logger.warning(
            "protection: module list for session %s was empty — using the conservative code extent",
            session_id,
        )
        return None
    return modules
