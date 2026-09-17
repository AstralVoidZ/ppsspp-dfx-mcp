"""Prompt-argument completions (`completion/complete`).

Registered via `@mcp.completion()`; imported by
`server.register_all_tools()` alongside resources and prompts.

**Scope**: MCP's completion protocol only supports `ref/prompt` and
`ref/resource` — tool arguments get no completion (their value hints live
in each parameter's `Field(description=...)`). Here we serve the `address`
argument of the two memory wizards with candidates drawn from
`.ppsspp-dfx/config/addresses.yaml`, so an Agent composing a prompt does
not have to guess the address format.

**Why only hex values, never symbol names**: `Completion.values` is
`list[str]` with no display-label field (the protocol has no
`CompletionItem` in SDK 2.2.0), and whatever is returned is what the
caller sees *as the argument value*. The wizards interpolate `{address}`
straight into `ppsspp_read_memory(address=...)` / `ppsspp_breakpoint(...)`,
so a symbol name like `state_probes.game_mode` would produce a broken
prompt. Only directly-usable strings qualify — see `_is_runtime_address`.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.types import Completion, PromptReference

from ppsspp_dfx_mcp import config
from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.server import mcp

logger = logging.getLogger(__name__)

__all__ = ["address_candidates", "complete"]

# Prompts whose `address` argument we complete.
_ADDRESS_PROMPTS = frozenset({"memory-breakpoint-wizard", "memory-trace-wizard"})

# Protocol ceiling on `Completion.values`.
_MAX_VALUES = 100

# Bands of *runtime* addresses, i.e. values that can be pasted into a tool
# call as-is. This project's addresses.yaml mixes address spaces:
#   - runtime user memory / module images: 0x0880_0000..0x0A00_0000
#     (top.prx loads at 0x08804000; its BSS and the game heap climb from
#     there — every `hazard_regions` boundary and `known_modules` entry
#     lands in this band)
#   - VRAM framebuffers: 0x0400_0000 / 0x0408_8000
# Everything else in the file is a *different* space and must not be
# offered: `known_render_vars` / `known_functions` / `text_render_aux`
# hold IDA offsets, `julian_dat_layout` / `eboot_ptr_tables` hold
# file-internal offsets, `top_base.ida` is a base-relative zero, and
# `ppsspp_ir_gotchas.breakpoint_stale_marker` is a marker *value*
# (0x68000194), not an address.
#
# A section allowlist would rot every time addresses.yaml grows; a band
# test instead encodes the file's own stated convention ("边界均为运行时
# 地址", hazard_regions header) and stays correct by construction.
_RUNTIME_BANDS: tuple[tuple[int, int], ...] = (
    (0x08800000, 0x0A000000),
    (0x04000000, 0x04200000),
)


def _is_runtime_address(value: Any) -> bool:
    """True when `value` looks like a PSP runtime address (KUSEG/VRAM)."""
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return any(lo <= value < hi for lo, hi in _RUNTIME_BANDS)


def _collect_runtime_addresses(node: Any, seen: set[int]) -> None:
    """Walk the parsed YAML and collect in-band ints, deduped via `seen`."""
    if isinstance(node, dict):
        for value in node.values():
            _collect_runtime_addresses(value, seen)
    elif isinstance(node, list):
        for value in node:
            _collect_runtime_addresses(value, seen)
    elif _is_runtime_address(node):
        seen.add(node)


def address_candidates(prefix: str = "") -> tuple[list[str], int]:
    """Return (matching addresses, total matches) for a prefix query.

    Matching is prefix-based on lowercase hex, and accepts the query with
    or without its leading `0x` / leading zeros — every runtime address
    here begins `0x0`, so an Agent typing `8804` should still find
    `0x08804000`.

    Returns `([], 0)` when nothing matches or the config cannot be read —
    a completion miss is not an error (it just means "no suggestion").
    """
    seen: set[int] = set()
    try:
        _collect_runtime_addresses(config.addresses(), seen)
    except Exception as e:  # pragma: no cover — mirrors list_addresses' guard
        logger.warning("completion: failed to load addresses.yaml: %s", e)
        return ([], 0)

    query = prefix.strip().lower().removeprefix("0x")
    matches: list[str] = []
    for value in sorted(seen):
        digits = f"{value:08x}"
        if query and not (digits.startswith(query) or digits.lstrip("0").startswith(query)):
            continue
        matches.append(format_address(value))
    return (matches[:_MAX_VALUES], len(matches))


@mcp.completion()
async def complete(ref: Any, argument: Any, context: Any = None) -> Completion | None:
    """`completion/complete` handler — serves the wizards' `address` argument.

    Returning `None` (rather than an empty `Completion`) is the SDK's
    "no suggestion" path; it is normalised to an empty values list
    upstream. Non-address arguments and non-prompt refs therefore need no
    special casing — see `prompt-argument-completions` spec.
    """
    if not isinstance(ref, PromptReference):
        # ref/resource: our two snapshot resources are plain URIs with no
        # template variables to complete.
        return None
    if ref.name not in _ADDRESS_PROMPTS or argument.name != "address":
        return None

    matches, total = address_candidates(argument.value)
    if not matches:
        return None
    return Completion(
        values=matches,
        total=total,
        has_more=total > len(matches),
    )
