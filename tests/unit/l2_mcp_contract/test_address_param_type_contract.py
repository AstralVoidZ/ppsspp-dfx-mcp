"""L2 MCP contract: tool wrappers expose `address` params as `str`.

Anchors the str-typed address migration (2026-07-27). Background:
- LLM Agents repeatedly mis-converted hex↔decimal when tool params were
  typed `int` (JSON has no `0x` literal, so the Agent had to emit
  `142606336` for `0x08804000` — error-prone).
- Fix: type all `address` / `start_addr` / `end_addr` / `end` params as
  `str` so FastMCP exports JSON Schema `{"type": "string"}`. The LLM
  sees "string" and naturally emits hex strings like "0x08804000".
- parse_address / parse_value (in ppsspp_dfx_mcp.address) convert the
  str back to int for DebugClient.

L2 anchor: tool wrapper signatures MUST declare address params as
`str` (or `str | None` for optional). If a tool regresses to `int`,
the JSON Schema flips back to `{"type": "integer"}` and the LLM
re-emits decimal conversions — the bug returns.

These tests use inspect.signature to check the type annotation of
each tool wrapper's address parameter. They do NOT call the tool
functions (that's L3's job).
"""

from __future__ import annotations

import inspect
import types
import typing
from typing import get_args, get_origin

import pytest

# mcp[cli] is a declared dependency; real Image import mirrors runtime.
from mcp.server.mcpserver import Image  # noqa: F401

from ppsspp_dfx_mcp.tools.assemble import assemble
from ppsspp_dfx_mcp.tools.breakpoint import breakpoint
from ppsspp_dfx_mcp.tools.memory import disassemble, read_memory, write_memory
from ppsspp_dfx_mcp.tools.query import query
from ppsspp_dfx_mcp.tools.scan import scan
from ppsspp_dfx_mcp.tools.search_disasm import search_disasm
from ppsspp_dfx_mcp.tools.search_memory_info import search_memory_info
from ppsspp_dfx_mcp.tools.state_observer import state_observer
from ppsspp_dfx_mcp.tools.step import step


def _get_param_annotation(func, param_name: str):
    """Extract the resolved type annotation of a function parameter.

    Uses typing.get_type_hints(include_extras=True) to resolve string
    annotations (PEP 563 — `from __future__ import annotations` makes
    all annotations strings at runtime). Returns the actual type object
    (e.g., `Annotated[str, Field(...)]` or `str`).

    Raises AssertionError if the parameter doesn't exist.
    """
    sig = inspect.signature(func)
    assert param_name in sig.parameters, f"{func.__name__} should have a `{param_name}` parameter"
    # Resolve string annotations to actual type objects.
    # include_extras=True keeps Annotated[...] wrapper (needed to inspect
    # the inner type).
    hints = typing.get_type_hints(func, include_extras=True)
    assert param_name in hints, (
        f"{func.__name__}.{param_name} has no resolved type hint "
        f"(annotation: {sig.parameters[param_name].annotation!r})"
    )
    return hints[param_name]


def _is_str_annotation(annotation) -> bool:
    """Check if an annotation is `str` or `str | None` (Optional[str]).

    Handles nested wrappers:
    - `str` (plain)
    - `str | None` (PEP 604 union)
    - `Optional[str]` / `Union[str, None]` (typing.Union form)
    - `Annotated[str, ...]` (Pydantic Field form)
    - `Annotated[str | None, ...]` (optional with Field)
    - `Optional[Annotated[str | None, ...]]` (nested Optional+Annotated,
      produced by search_memory_info's `address: Optional[Annotated[str | None, Field(...)]] = None`)
    """
    seen = set()
    while True:
        # Prevent infinite loops on malformed input
        if id(annotation) in seen:
            return False
        seen.add(id(annotation))

        origin = get_origin(annotation)

        # Unwrap Annotated[X, ...] → X
        if origin is typing.Annotated:
            args = get_args(annotation)
            if not args:
                return False
            annotation = args[0]
            continue

        # Plain str
        if annotation is str:
            return True

        # str | None (PEP 604) → types.UnionType
        if hasattr(types, "UnionType") and isinstance(annotation, types.UnionType):
            args = get_args(annotation)
            return str in args and type(None) in args

        # typing.Union[str, None] / Optional[str] / Optional[X]
        # If it's Union with None, unwrap to the non-None branch and
        # re-check (handles Optional[Annotated[...]] nesting).
        if origin is typing.Union:
            args = get_args(annotation)
            non_none = [a for a in args if a is not type(None)]
            if not non_none:
                return False
            if len(non_none) == 1 and non_none[0] is str:
                return True
            # Unwrap Optional[X] → X and re-check (e.g., Optional[Annotated[...]])
            if len(non_none) == 1:
                annotation = non_none[0]
                continue
            return False

        return False


# ============================================================================
# Required address params: typed as `str` (not int)
# ============================================================================


class TestRequiredAddressParamsAreStr:
    """Tools with required `address` param must type it as `str`.

    These tools take a memory address and pass it to DebugClient.
    Pre-migration: params were `int` → JSON Schema `{"type": "integer"}`
    → LLM emitted decimal conversions → wrong addresses.
    Post-migration: params are `str` → JSON Schema `{"type": "string"}`
    → LLM emits "0x08804000" directly.
    """

    @pytest.mark.parametrize(
        "func,param_name",
        [
            (read_memory, "address"),
            (write_memory, "address"),
            (disassemble, "address"),
            (breakpoint, "address"),
            (assemble, "address"),
            (search_disasm, "address"),
            (state_observer, "address"),
            (step, "address"),
        ],
    )
    def test_address_param_is_str(self, func, param_name):
        """L2 anchor: address param is typed `str` (not int)."""
        annotation = _get_param_annotation(func, param_name)
        assert _is_str_annotation(annotation), (
            f"{func.__name__}.{param_name} must be typed `str` (or "
            f"`str | None`), got: {annotation!r}. If this regresses to "
            f"`int`, the LLM will re-emit decimal conversions — the "
            f"address mis-conversion bug returns."
        )


class TestOptionalAddressParamsAreStr:
    """Tools with optional `address` param (default None) must type as
    `str | None`.

    These tools accept address optionally (e.g., query with action=
    'func_remove' uses address; other actions don't).
    """

    @pytest.mark.parametrize(
        "func,param_name",
        [
            (query, "address"),
            (search_memory_info, "address"),
        ],
    )
    def test_optional_address_param_is_str(self, func, param_name):
        """L2 anchor: optional address param is typed `str | None`."""
        annotation = _get_param_annotation(func, param_name)
        assert _is_str_annotation(annotation), (
            f"{func.__name__}.{param_name} must be typed `str | None` "
            f"(optional address), got: {annotation!r}. Regression to "
            f"`int | None` would re-trigger the decimal-conversion bug."
        )


class TestRangeParamsAreStr:
    """Tools with range params (start_addr / end_addr / end) must type
    them as `str`.

    These params define a memory range for scan / search operations.
    Same str-migration rationale as `address`.
    """

    @pytest.mark.parametrize(
        "func,param_name",
        [
            (scan, "start_addr"),
            (scan, "end_addr"),
            (search_disasm, "end"),
        ],
    )
    def test_range_param_is_str(self, func, param_name):
        """L2 anchor: range param (start_addr/end_addr/end) is typed `str`."""
        annotation = _get_param_annotation(func, param_name)
        assert _is_str_annotation(annotation), (
            f"{func.__name__}.{param_name} must be typed `str` (range "
            f"address), got: {annotation!r}. Range addresses follow the "
            f"same str-migration as `address`."
        )
