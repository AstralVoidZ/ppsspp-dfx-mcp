"""Shared helpers for parsing cpu.getAllRegs response.

Both `service/debug_client.py` and `core/stepping.py` previously had
identical `_extract_pc` static methods. Centralizing them here removes
the duplication and gives a natural home for future register-extraction
helpers (extract_sp, extract_ra, etc.).

Placement in `core/`: this module is a pure function over PPSSPP's
protocol response shape — no transport, no stepping, no I/O. It depends
on nothing except the standard library. The pipeflow rule
(`tools → views → models`) is respected: callers in `service/` and
`core/` may import this module freely.
"""

from __future__ import annotations

from typing import Any


def extract_pc(regs: dict[str, Any]) -> int:
    """Extract the PC register value from a cpu.getAllRegs response.

    PPSSPP's cpu.getAllRegs returns a dict with a "categories" list. Each
    category has a name (e.g. "GPR", "FPU", "VFPU") plus parallel
    ``registerNames`` and ``uintValues`` lists. The PC register lives
    in the GPR category.

    Args:
        regs: Parsed ``cpu.getAllRegs`` response dict.

    Returns:
        The PC value as an int, or 0 if the GPR category or ``pc``
        register was not found.
    """
    for cat in regs.get("categories", []):
        if cat.get("name") == "GPR":
            names = cat.get("registerNames", [])
            vals = cat.get("uintValues", [])
            if "pc" in names:
                return int(vals[names.index("pc")])
    return 0


# ── Register name normalization ─────────────────────────────────────────────
#
# PPSSPP's cpu.getReg / cpu.setReg only accept the ABI names produced by
# MIPSDebugInterface::GetRegName (MIPSDebugInterface.cpp:280-290) plus the
# special-cased "pc" / "hi" / "lo". Numeric-style names such as "r3" are
# rejected with "Invalid 'name' parameter".

GPR_ABI_NAMES: tuple[str, ...] = (
    "zero", "at", "v0", "v1",
    "a0", "a1", "a2", "a3",
    "t0", "t1", "t2", "t3",
    "t4", "t5", "t6", "t7",
    "s0", "s1", "s2", "s3",
    "s4", "s5", "s6", "s7",
    "t8", "t9", "k0", "k1",
    "gp", "sp", "fp", "ra",
)

_GPR_BY_NUMERIC: dict[str, str] = {
    f"r{i}": abi for i, abi in enumerate(GPR_ABI_NAMES)
}


def normalize_reg_name(name: str) -> str:
    """Normalize a register name to PPSSPP's exact expected form.

    PPSSPP only understands the lowercase ABI names ("a0"/"v0"/"t9"/...)
    plus the special-cased "pc"/"hi"/"lo". Numeric-style names ("r3") map
    to their ABI name ("v1").

    A leading ``$`` is stripped and the name is
    lowercased unconditionally — ``"$ra"`` → ``"ra"``, ``"PC"`` → ``"pc"``.
    Previously both were forwarded verbatim and PPSSPP rejected them with
    "Invalid 'name' parameter" (the docstring claimed lowercasing that the
    code never did).
    """
    low = name.strip().lower().lstrip("$")
    if low in _GPR_BY_NUMERIC:
        return _GPR_BY_NUMERIC[low]
    return low
