"""L2 MCP contract: views layer exposes address fields as `str` (hex string).

Anchors the str-typed address output migration (2026-07-27). Background:
- Tool params (input) were migrated to `str` (hex string) so the LLM
  emits "0x08804000" directly instead of error-prone decimal conversions.
- Views layer (output) must match: address fields typed as `str`,
  formatted via `format_address()` in from_result. This symmetry (hex
  string in → hex string out) prevents the LLM from doing hex↔decimal
  conversions in either direction.

L2 anchor: view model fields for addresses/PC MUST be typed `str`. If a
view regresses to `int`, the JSON response flips back to numeric and
the LLM has to convert the response address back to hex to pass it
into the next tool call — re-introducing the bug.

Tested view models (all in ppsspp_dfx_mcp.views):
- MemoryReadResponse.address
- MemoryWriteResponse.address
- DisassemblyResponse.address
- BreakpointResponse.address
- AssembleResponse.address
- SearchDisasmResponse.address / .end
- ProbeObservationView.address
- StateProbeView.address
- StepResponse.address / .pc / .related_address
- AddressConversionResponse.original / .converted / .top_base_ppsspp /
  .top_base_ida
- GetPcResponse.pc

Not covered here (kept as int deliberately — these are data values, not
addresses):
- ProbeObservationView.value (raw unsigned value read, 1/2/4 bytes)
- MemoryWriteResponse.value (u32 data value written)

These tests use typing.get_type_hints(include_extras=True) to resolve
string annotations (PEP 563 — `from __future__ import annotations`).
"""

from __future__ import annotations

import typing
from typing import get_args, get_origin

import pytest

from ppsspp_dfx_mcp.views.analyze import AddressConversionResponse
from ppsspp_dfx_mcp.views.assemble import AssembleResponse
from ppsspp_dfx_mcp.views.breakpoint import BreakpointResponse
from ppsspp_dfx_mcp.views.memory import (
    DisassemblyResponse,
    MemoryReadResponse,
    MemoryWriteResponse,
)
from ppsspp_dfx_mcp.views.query import GetPcResponse
from ppsspp_dfx_mcp.views.search_disasm import SearchDisasmResponse
from ppsspp_dfx_mcp.views.state_observer import (
    ProbeObservationView,
    StateProbeView,
)
from ppsspp_dfx_mcp.views.step import StepResponse


def _is_str_field(annotation) -> bool:
    """Check if an annotation is `str` (handles Annotated wrappers)."""
    # Unwrap Annotated[X, ...] → X
    origin = get_origin(annotation)
    if origin is typing.Annotated:
        args = get_args(annotation)
        if args:
            annotation = args[0]

    return annotation is str


def _get_field_type(model_cls, field_name: str):
    """Extract the resolved type annotation of a Pydantic model field.

    Uses typing.get_type_hints to resolve string annotations from
    `from __future__ import annotations`. Returns the actual type.

    Raises AssertionError if the field doesn't exist.
    """
    hints = typing.get_type_hints(model_cls, include_extras=True)
    assert field_name in hints, f"{model_cls.__name__} should have a `{field_name}` field"
    return hints[field_name]


# ============================================================================
# Required address fields (single address per response)
# ============================================================================


class TestViewAddressFieldsAreStr:
    """Views with a single `address` field must type it as `str`.

    These views return a primary address (read/write/disassemble/
    breakpoint/assemble/search/probe/step target). All must be hex
    strings so the LLM can round-trip the address without conversion.
    """

    @pytest.mark.parametrize(
        "model_cls,field_name",
        [
            (MemoryReadResponse, "address"),
            (MemoryWriteResponse, "address"),
            (DisassemblyResponse, "address"),
            (BreakpointResponse, "address"),
            (AssembleResponse, "address"),
            (SearchDisasmResponse, "address"),
            (ProbeObservationView, "address"),
            (StateProbeView, "address"),
            (StepResponse, "address"),
        ],
    )
    def test_address_field_is_str(self, model_cls, field_name):
        """L2 anchor: address field is typed `str` (not int)."""
        annotation = _get_field_type(model_cls, field_name)
        assert _is_str_field(annotation), (
            f"{model_cls.__name__}.{field_name} must be typed `str` (hex "
            f"string output), got: {annotation!r}. If this regresses to "
            f"`int`, the LLM receives a numeric address in the response "
            f"and must convert it back to hex to pass into the next tool "
            f"call — re-introducing the address mis-conversion bug."
        )


# ============================================================================
# PC and related_address fields
# ============================================================================


class TestViewPcFieldsAreStr:
    """Views exposing program counter / related address must type as `str`.

    These are addresses (program counter / temp breakpoint target), not
    data values, so they follow the same str-migration.
    """

    @pytest.mark.parametrize(
        "model_cls,field_name",
        [
            (StepResponse, "pc"),
            (StepResponse, "related_address"),
            (GetPcResponse, "pc"),
        ],
    )
    def test_pc_field_is_str(self, model_cls, field_name):
        """L2 anchor: PC / related_address fields are typed `str`."""
        annotation = _get_field_type(model_cls, field_name)
        assert _is_str_field(annotation), (
            f"{model_cls.__name__}.{field_name} must be typed `str` (hex "
            f"string output for an address), got: {annotation!r}."
        )


# ============================================================================
# Address conversion response — all four address fields
# ============================================================================


class TestAddressConversionResponseFieldsAreStr:
    """AddressConversionResponse exposes 4 address fields — all must be `str`.

    - original: input address (was `int`, now `str` for input/output symmetry)
    - converted: output address
    - top_base_ppsspp: top.prx PPSSPP base (constant)
    - top_base_ida: top.prx IDA base (constant)

    If any regresses to `int`, the conversion response becomes asymmetric
    (hex string in → numeric out), forcing the LLM to convert.
    """

    @pytest.mark.parametrize(
        "field_name",
        ["original", "converted", "top_base_ppsspp", "top_base_ida"],
    )
    def test_conversion_field_is_str(self, field_name):
        """L2 anchor: all 4 address fields in AddressConversionResponse are `str`."""
        annotation = _get_field_type(AddressConversionResponse, field_name)
        assert _is_str_field(annotation), (
            f"AddressConversionResponse.{field_name} must be typed `str` "
            f"(hex string output), got: {annotation!r}."
        )


# ============================================================================
# Search range end address
# ============================================================================


class TestSearchDisasmEndFieldIsStr:
    """SearchDisasmResponse.end is an address (search range end), must be `str`."""

    def test_end_field_is_str(self):
        """L2 anchor: SearchDisasmResponse.end is typed `str` (address)."""
        annotation = _get_field_type(SearchDisasmResponse, "end")
        assert _is_str_field(annotation), (
            f"SearchDisasmResponse.end must be typed `str` (hex string "
            f"output for an address), got: {annotation!r}."
        )


# ============================================================================
# Behavior: from_result formats int → hex string
# ============================================================================


class TestFromResultFormatsAddressAsHex:
    """from_result class methods must call format_address to convert int → str.

    Anchors the formatting behavior (not just the type): the view's
    `from_result` must pass the int address through `format_address()`
    before assigning to the str field. If from_result regresses to
    passing the raw int, Pydantic would coerce it to its decimal string
    form (e.g. "142606336") — re-introducing the decimal-conversion
    problem at the output layer.
    """

    @pytest.mark.parametrize(
        "model_cls,model_module",
        [
            (MemoryReadResponse, "ppsspp_dfx_mcp.views.memory"),
            (MemoryWriteResponse, "ppsspp_dfx_mcp.views.memory"),
            (DisassemblyResponse, "ppsspp_dfx_mcp.views.memory"),
            (BreakpointResponse, "ppsspp_dfx_mcp.views.breakpoint"),
            (AssembleResponse, "ppsspp_dfx_mcp.views.assemble"),
            (SearchDisasmResponse, "ppsspp_dfx_mcp.views.search_disasm"),
            (StepResponse, "ppsspp_dfx_mcp.views.step"),
            (AddressConversionResponse, "ppsspp_dfx_mcp.views.analyze"),
            (GetPcResponse, "ppsspp_dfx_mcp.views.query"),
            (ProbeObservationView, "ppsspp_dfx_mcp.views.state_observer"),
            (StateProbeView, "ppsspp_dfx_mcp.views.state_observer"),
        ],
    )
    def test_from_result_source_calls_format_address(self, model_cls, model_module):
        """L2 anchor: view module imports and calls `format_address`.

        We inspect the module source (not the class source) because
        `format_address` is imported at module level and called inside
        the classmethod. This catches removal of the import or the call.
        """
        import inspect

        module = __import__(model_module, fromlist=["__source__"])
        src = inspect.getsource(module)

        # 1. Module imports format_address from ppsspp_dfx_mcp.address
        assert "format_address" in src, (
            f"{model_module} must import `format_address` from "
            f"ppsspp_dfx_mcp.address — needed to format int address as "
            f"hex string in from_result. If this import is removed, "
            f"from_result will pass raw ints to str fields (Pydantic "
            f"coerces to decimal string — bug returns)."
        )

        # 2. from_result calls format_address at least once
        #    (count >= 1 — some views call it multiple times for multiple fields)
        call_count = src.count("format_address(")
        # Subtract the import line (1 occurrence from `from ... import format_address`)
        # The import statement contains "format_address" but not "format_address("
        # (it has "format_address" followed by newline or comma).
        # Counting "format_address(" specifically catches calls.
        assert call_count >= 1, (
            f"{model_module} must call `format_address(...)` at least once "
            f"in from_result (found {call_count} calls). Without this call, "
            f"the str-typed address field would receive a raw int."
        )


# ============================================================================
# Smoke test: instantiate a view with a real int address, verify hex output
# ============================================================================


class TestFromResultProducesHexOutput:
    """End-to-end smoke: from_result(int address) → str "0x...".

    Uses real model/dataclass instances where possible. For models that
    need complex inner objects, we use simple stubs.
    """

    def test_memory_read_response_address_is_hex(self):
        """from_result with int address 0x08804000 → str '0x08804000'."""
        from ppsspp_dfx_mcp.models.memory import MemoryReadResult

        result = MemoryReadResult(
            action="read_u32",
            address=0x08804000,
            value=42,
            size=4,
        )
        view = MemoryReadResponse.from_result(result)
        assert view.address == "0x08804000", (
            f"from_result should format 0x08804000 as '0x08804000', got {view.address!r}"
        )
        assert isinstance(view.address, str)

    def test_step_response_pc_is_hex(self):
        """from_result with int pc 0xDEADBEEF → str '0xDEADBEEF'."""
        from ppsspp_dfx_mcp.models.step import StepResult

        result = StepResult(
            action="into",
            address=0,
            pc=0xDEADBEEF,
            ticks=12345.0,
            reason="cpu.stepInto",
            related_address=0,
        )
        view = StepResponse.from_result(result)
        assert view.pc == "0xDEADBEEF", (
            f"from_result should format pc 0xDEADBEEF as '0xDEADBEEF', got {view.pc!r}"
        )
        assert view.address == "0x00000000"
        assert view.related_address == "0x00000000"

    def test_address_conversion_all_fields_hex(self):
        """from_result formats all 4 address fields as hex strings."""
        from ppsspp_dfx_mcp.models.analyze import AddressConversionResult

        result = AddressConversionResult(
            original=0x00010000,
            converted=0x08814000,
            mode="ida_to_ppsspp",
            top_base_ppsspp=0x08804000,
            top_base_ida=0x00010000,
        )
        view = AddressConversionResponse.from_result(result)
        assert view.original == "0x00010000"
        assert view.converted == "0x08814000"
        assert view.top_base_ppsspp == "0x08804000"
        assert view.top_base_ida == "0x00010000"
