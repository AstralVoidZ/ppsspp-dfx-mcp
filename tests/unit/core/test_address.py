"""Unit tests for the address parsing layer (parse_address / parse_value).

Anchors the contract that all MCP tool `address` / `value` params (typed
`str` in tool wrappers) parse back to the `int` that DebugClient /
WsTransport expects.

Background: tool params are typed `str` (not `int`) so FastMCP exports
JSON Schema `{"type": "string"}` — this steers the LLM Agent toward
emitting hex strings like "0x08804000" instead of error-prone decimal
conversions. parse_address / parse_value is the single place that
converts the str back to int.

Accepted formats (see address.py module docstring):
- "0x08804000" / "0X08804000" — hex with prefix (preferred)
- "142606336" — decimal string (rare, accepted for completeness)
- 0x08804000 / 142606336 — raw int (internal callers, tests)
- "" — empty string, parsed as 0 (for optional params with default "")

Rejected:
- "08804000" — no `0x` prefix on non-decimal-looking string is ambiguous
- None — caller must pass a value; use "" for "not provided"
- "0xZZ" — invalid hex digits
- non-str/non-int types (list, dict, etc.)
"""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp.address import (
    Address,
    Value,
    format_address,
    parse_address,
    parse_value,
)
from ppsspp_dfx_mcp.errors import ToolError


class TestParseAddressHex:
    """parse_address accepts hex strings with `0x` prefix."""

    @pytest.mark.parametrize(
        "s,expected",
        [
            ("0x08804000", 0x08804000),
            ("0X08804000", 0x08804000),  # uppercase prefix
            ("0xDeadBeef", 0xDEADBEEF),  # mixed case digits
            ("0x00000000", 0),  # all zeros
            ("0x1", 1),  # short form
            ("  0x08804000  ", 0x08804000),  # leading/trailing whitespace
        ],
    )
    def test_parses_hex_string(self, s, expected):
        assert parse_address(s) == expected

    def test_int_passthrough(self):
        """Raw int (internal callers) is passed through unchanged."""
        assert parse_address(0x08804000) == 0x08804000
        assert parse_address(142606336) == 142606336
        assert parse_address(0) == 0


class TestParseAddressDecimal:
    """parse_address accepts decimal strings (no 0x prefix)."""

    @pytest.mark.parametrize(
        "s,expected",
        [
            ("142606336", 142606336),  # decimal equivalent of 0x08804000
            ("0", 0),
            ("  12345  ", 12345),  # whitespace stripped
        ],
    )
    def test_parses_decimal_string(self, s, expected):
        assert parse_address(s) == expected


class TestParseAddressEmpty:
    """parse_address treats empty string as 0 (for optional params)."""

    @pytest.mark.parametrize("s", ["", "   ", "\t\n"])
    def test_empty_string_is_zero(self, s):
        assert parse_address(s) == 0


class TestParseAddressInvalid:
    """parse_address rejects malformed input with AddrInvalid.

    W10c fix (2026-09-06): malformed input is an INPUT error, so the
    code changed from "INTERNAL" to "ADDR_INVALID" (the business code
    tools/memory.py already used for out-of-range values).

    Note: "08804000" (digits only, no 0x prefix) is ACCEPTED as decimal
    8804000 — it's a valid decimal string. Only strings that are neither
    valid decimal nor valid hex (with 0x prefix) are rejected.
    """

    @pytest.mark.parametrize(
        "s,reason",
        [
            ("0x", "empty hex digits after 0x prefix"),
            ("0xZZ", "invalid hex digits"),
            ("0x12G4", "mixed valid/invalid hex digits"),
            ("hello", "neither decimal nor hex"),
            ("123abc", "looks like hex but no 0x prefix, has letters"),
        ],
    )
    def test_invalid_string_raises_tool_error(self, s, reason):
        with pytest.raises(ToolError) as exc_info:
            parse_address(s)
        assert exc_info.value.code == "ADDR_INVALID", (
            f"parse_address({s!r}) should raise AddrInvalid ({reason})"
        )

    @pytest.mark.parametrize("invalid", [None, [], {}, 1.5, object()])
    def test_invalid_type_raises_tool_error(self, invalid):
        """Non-str/non-int inputs are rejected with INTERNAL error."""
        with pytest.raises(ToolError) as exc_info:
            parse_address(invalid)  # type: ignore[arg-type]
        assert exc_info.value.code == "ADDR_INVALID"

    def test_error_message_mentions_hex_format(self):
        """Error message should hint at the expected '0x08804000' format."""
        with pytest.raises(ToolError) as exc_info:
            parse_address("hello")
        msg = str(exc_info.value)
        # Either mentions '0x' prefix requirement or hex format
        assert "0x" in msg or "hex" in msg.lower(), (
            f"Error message should mention '0x' prefix or hex format; got: {msg!r}"
        )

    def test_error_message_includes_param_name(self):
        """parse_address errors should mention 'address' in the message."""
        with pytest.raises(ToolError) as exc_info:
            parse_address("0xZZ")
        assert "address" in str(exc_info.value).lower(), (
            f"parse_address error should mention 'address' param name; got: {exc_info.value!s}"
        )


class TestParseValue:
    """parse_value mirrors parse_address but for data values (write_memory).

    Used for the `value` param in write_memory and other data-value params.
    Same parsing rules as parse_address — kept as a separate function for
    semantic clarity (a data value is conceptually different from an
    address, even if both are int).
    """

    @pytest.mark.parametrize(
        "v,expected",
        [
            ("0xDEADBEEF", 0xDEADBEEF),
            ("0xdeadbeef", 0xDEADBEEF),
            ("3735928559", 3735928559),
            (0xDEADBEEF, 0xDEADBEEF),
            (0, 0),
            ("", 0),
        ],
    )
    def test_parses_value(self, v, expected):
        assert parse_value(v) == expected

    def test_invalid_value_raises_tool_error(self):
        with pytest.raises(ToolError) as exc_info:
            parse_value("0xZZ")
        assert exc_info.value.code == "ADDR_INVALID"

    def test_error_message_mentions_value_param_name(self):
        """parse_value errors should mention 'value' (not 'address')."""
        with pytest.raises(ToolError) as exc_info:
            parse_value("0xZZ")
        msg = str(exc_info.value).lower()
        assert "value" in msg, (
            f"parse_value error should mention 'value' param name; got: {exc_info.value!s}"
        )


class TestSemanticAliases:
    """Address and Value are semantic type aliases for str.

    These aliases exist so tool wrappers can write `address: Address`
    instead of `address: str` for self-documenting code. They must both
    be `str` at the type-system level so FastMCP exports
    `{"type": "string"}` in JSON Schema (the whole point of the str
    migration — steer the LLM toward hex strings).
    """

    def test_address_is_str(self):
        assert Address is str

    def test_value_is_str(self):
        assert Value is str


class TestFormatAddress:
    """format_address formats an int as a padded hex string.

    Contract:
    - Always returns "0x" + 8 uppercase hex digits (32-bit address space).
    - Masks input to 32 bits (negative ints / overflow wrap cleanly).
    - Inverse of parse_address: parse_address(format_address(n)) == n
      for all n in [0, 0xFFFFFFFF].
    - Used in views layer (from_result) so response address fields are
      hex strings, matching the input shape (Agent passes "0x..." in,
      Agent receives "0x..." out).
    """

    @pytest.mark.parametrize(
        "value,expected",
        [
            (0, "0x00000000"),
            (1, "0x00000001"),
            (0x08804000, "0x08804000"),
            (0xDEADBEEF, "0xDEADBEEF"),
            (0xFFFFFFFF, "0xFFFFFFFF"),
            (0x1234, "0x00001234"),  # zero-padded to 8 digits
        ],
    )
    def test_formats_int_as_hex_string(self, value, expected):
        assert format_address(value) == expected

    def test_returns_str(self):
        """format_address must return a str (not int, not bytes)."""
        result = format_address(0x08804000)
        assert isinstance(result, str), (
            f"format_address must return str, got {type(result).__name__}"
        )

    def test_zero_padded_to_8_digits(self):
        """All outputs are exactly 10 chars: '0x' + 8 hex digits."""
        for value in [0, 1, 0xFF, 0xFFFF, 0xFFFFFF, 0xFFFFFFFF]:
            result = format_address(value)
            assert len(result) == 10, (
                f"format_address({value:#x}) = {result!r} (len {len(result)}), "
                f"expected exactly 10 chars ('0x' + 8 hex digits)"
            )

    def test_uppercase_hex(self):
        """Hex digits A-F must be uppercase (matches IDA/PPSSPP convention)."""
        result = format_address(0xABCDEF12)
        assert result == "0xABCDEF12", f"expected uppercase '0xABCDEF12', got {result!r}"

    @pytest.mark.parametrize("value", [-1, -2, 0x100000000, -0x80000000])
    def test_masks_to_32_bits(self, value):
        """Negative ints / overflow wrap cleanly to 32-bit address space.

        format_address(-1) → "0xFFFFFFFF" (32-bit mask of -1).
        format_address(0x100000000) → "0x00000000" (overflow wraps to 0).
        """
        result = format_address(value)
        expected = f"0x{value & 0xFFFFFFFF:08X}"
        assert result == expected, f"format_address({value}) = {result!r}, expected {expected!r}"

    def test_inverse_of_parse_address(self):
        """Round-trip: parse_address(format_address(n)) == n for 0 ≤ n ≤ 0xFFFFFFFF."""
        for value in [0, 1, 0x08804000, 0xDEADBEEF, 0xFFFFFFFF]:
            formatted = format_address(value)
            parsed = parse_address(formatted)
            assert parsed == value, (
                f"round-trip failed: format_address({value:#x}) = {formatted!r}, "
                f"parse_address({formatted!r}) = {parsed}"
            )

    def test_inverse_of_parse_address_decimal_string(self):
        """parse_address also accepts decimal, but format_address always
        emits hex — round-trip still holds because parsed int matches.
        """
        value = 0x08804000
        formatted = format_address(value)
        # parsed via hex string
        assert parse_address(formatted) == value
        # parsed via equivalent decimal string
        assert parse_address(str(value)) == value


class TestRoundTripWithDebugClient:
    """parse_address output is compatible with DebugClient (int-taking).

    This anchors the bridge contract: tool wrappers accept str, parse
    via parse_address, then pass the resulting int to DebugClient
    methods (which send int to PPSSPP WS). If parse_address returned a
    non-int, DebugClient would break.
    """

    @pytest.mark.parametrize(
        "s,expected_int",
        [
            ("0x08804000", 0x08804000),
            ("0xDEADBEEF", 0xDEADBEEF),
            ("142606336", 142606336),
        ],
    )
    def test_parse_returns_int(self, s, expected_int):
        result = parse_address(s)
        assert isinstance(result, int), (
            f"parse_address must return int (DebugClient expects int); got {type(result).__name__}"
        )
        assert result == expected_int


class TestW13AddressRange:
    """W13 fix (2026-09-06): parse_address rejects out-of-32-bit values
    and bools instead of forwarding them (and having format_address
    silently mask the echo)."""

    def test_negative_address_rejected(self):
        with pytest.raises(ToolError, match="out of the 32-bit range") as e:
            parse_address(-5)
        assert e.value.code == "ADDR_INVALID"

    def test_oversized_address_rejected(self):
        with pytest.raises(ToolError, match="out of the 32-bit range"):
            parse_address("0x123456789")

    def test_boundary_values_accepted(self):
        assert parse_address(0) == 0
        assert parse_address(0xFFFFFFFF) == 0xFFFFFFFF
        assert parse_address("0xFFFFFFFF") == 0xFFFFFFFF

    def test_bool_rejected(self):
        with pytest.raises(ToolError):
            parse_address(True)  # type: ignore[arg-type]
