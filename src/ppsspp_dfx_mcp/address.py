"""Address & value parsing — unified hex/dec string → int conversion.

Why this module exists:
- JSON (the MCP wire protocol) has no `0x` literal. If tool params are
  typed `int`, the LLM Agent must mentally convert `0x08804000` →
  `142606336` before emitting the tool call — an error-prone step that
  caused repeated "wrong address" failures in practice.
- Typing params as `str` lets the Agent emit `"0x08804000"` directly,
  matching the hex convention used by IDA Pro, addresses.yaml, and
  every PPSSPP debug doc. This module is the single place that parses
  the string back to the `int` that DebugClient / WsTransport expects.

Accepted input formats (parse_address / parse_value):
- `"0x08804000"` / `"0X08804000"` — hex with prefix (preferred)
- `"142606336"` — decimal string (rare, but accepted for completeness)
- `0x08804000` / `142606336` — raw int (internal callers, tests)
- `""` — empty string, parsed as 0 (for optional params with default "")
- ⚠️ `"08804000"` — digits only, no prefix: parsed as **DECIMAL** (8804000),
  NOT as hex. See "The bare-digit trap" below.

Rejected:
- `None` — caller must pass a value; use "" for "not provided"
- `"0xZZ"` — invalid hex digits
- `"123abc"` — neither valid decimal nor hex-with-prefix

The bare-digit trap (why `"08804000"` is the dangerous input):
- An Agent that drops the `0x` from `0x08804000` is making the single most
  likely address mistake, and the result is not an error: 8804000 =
  `0x008656A0` is inside the 32-bit range, so every range check passes and the
  tool returns success for an address the caller never meant.
- The rule is deliberately "decimal first, no prefix" (see `_parse_int`), so
  this is *working as designed* — but it is silent, and the only defense is to
  write the `0x`. `instructions.py` and the ppsspp-dfx skill both call it out.
- Not fixable by rejection without breaking legitimate decimal strings: any
  all-digit string (e.g. `"88888888"`) is a valid decimal AND a valid hex, so
  no rule can pick the caller's intent. Hence: document, don't guess.

Why not Pydantic Annotated[int, BeforeValidator]?
- That would still export JSON Schema `{"type": "integer"}`, so the LLM
  would keep emitting decimal numbers. We need `{"type": "string"}` to
  steer the LLM toward hex strings — hence params are typed `str` and
  parsed explicitly at the tool entry point.
"""

from __future__ import annotations

from typing import Any

from ppsspp_dfx_mcp.errors import AddrInvalid

__all__ = [
    "Address",
    "Value",
    "parse_address",
    "parse_value",
    "format_address",
    "format_address_fields",
    "ADDRESS_FIELD_NAMES",
]

# Semantic type aliases. Both are `str` at the type-system level so
# FastMCP exports `{"type": "string"}` in JSON Schema — the LLM sees
# "string" and naturally emits hex strings like "0x08804000".
Address = str
Value = str

# Sentinel for "no address provided" — callers pass "" for optional
# address params (default ""). Distinguished from None to keep the
# JSON Schema as `str` (not `str | None`) so the LLM never has to
# decide between null and "".
_EMPTY = ""


def _parse_int(value: str | int, *, param_name: str) -> int:
    """Core parser shared by parse_address / parse_value.

    Args:
        value: str (hex "0x..." / dec "123") or int.
        param_name: parameter name for error messages (e.g. "address").

    Returns:
        Parsed int value.

    Raises:
        AddrInvalid: malformed input (code="ADDR_INVALID" — W10c fix:
            a malformed parameter is an input error, not an internal
            server fault).
    """
    if isinstance(value, bool):
        # bool is an int subclass — True would otherwise parse as
        # address 0x00000001 silently.
        raise AddrInvalid(
            f"{param_name} must be a hex string (e.g. '0x08804000') or int; got bool",
        )
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise AddrInvalid(
            f"{param_name} must be a hex string (e.g. '0x08804000') or int; "
            f"got {type(value).__name__}",
        )
    s = value.strip()
    if s == _EMPTY:
        return 0
    lowered = s.lower()
    if lowered.startswith("0x"):
        digits = s[2:]
        if not digits or not all(c in "0123456789abcdefABCDEF" for c in digits):
            raise AddrInvalid(
                f"{param_name} has invalid hex digits: {value!r} "
                f"(expected format like '0x08804000')",
            )
        return int(digits, 16)
    # No 0x prefix — try decimal string first; if it has non-decimal
    # chars, the user probably forgot the 0x prefix.
    try:
        return int(s, 10)
    except ValueError:
        raise AddrInvalid(
            f"{param_name} is not a valid decimal or hex string: {value!r} "
            f"(hex values must use '0x' prefix, e.g. '0x08804000')",
        ) from None


def parse_address(address: str | int) -> int:
    """Parse a memory address from str/int to int.

    Rejects negative and >32-bit values — the
    previous implementation accepted them verbatim (and the WS request
    then carried an address that format_address would silently mask in
    the response echo, e.g. request 0x1_08804000, response "0x08804000").

    Use for all `address` / `start_addr` / `end_addr` / `end` params.
    See module docstring for accepted formats.

    Raises:
        AddrInvalid: negative, >0xFFFFFFFF, bool, or malformed input.
    """
    value = _parse_int(address, param_name="address")
    if value < 0 or value > 0xFFFFFFFF:
        raise AddrInvalid(f"address out of the 32-bit range: {value!r} (expected 0..0xFFFFFFFF)")
    return value


def parse_value(value: str | int) -> int:
    """Parse a data value (e.g. write_memory `value`) from str/int to int.

    Same parsing rules as parse_address — hex strings (preferred) and
    decimal strings are both accepted. Use this for any numeric param
    that represents a data value rather than a memory address.
    """
    return _parse_int(value, param_name="value")


def format_address(value: int) -> str:
    """Format an int address/value as a hex string.

    Inverse of parse_address: int → "0x{VALUE:08X}".

    Use in views layer (from_result) to expose addresses as hex strings,
    so the response shape matches the input shape (Agent passes "0x..."
    in, Agent receives "0x..." out). This symmetry avoids the LLM
    converting between hex and decimal in either direction.

    Always pads to 8 hex digits (32-bit address space), matching
    addresses.yaml convention and IDA/PPSSPP debug output.

    Examples:
        format_address(0x08804000) → "0x08804000"
        format_address(0)          → "0x00000000"
        format_address(0xDEADBEEF) → "0xDEADBEEF"
    """
    return f"0x{value & 0xFFFFFFFF:08X}"

# ── Address field name whitelist ───────────────────────────────────────
#
# Field names that semantically represent a memory address (not a size,
# count, or metadata int). Used by format_address_fields to decide which
# int values in a PPSSPP raw response dict to convert to hex strings.
#
# Membership is by exact name match (case-sensitive). PPSSPP's WS protocol
# uses lowercase snake_case for most fields; camelCase appears only in
# legacy broadcast events (entryPoint). Both are listed.
#
# Deliberately excludes: size, initialStackSize, currentStackSize, priority,
# waitType, status, id, count, ticks, offset, bytes_written, ticket, etc.
# — these are NOT addresses and hex formatting would mislead the caller.
ADDRESS_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "pc",               # program counter
        "entry",            # thread entry address
        "address",          # generic address field
        "start",            # range start address
        "end",              # range end address
        "related_address",  # breakpoint related address
        "entryPoint",       # camelCase legacy broadcast
        "entryAddr",        # alternate entry naming
        "loadAddress",      # module load address (camelCase)
        "load_address",     # module load address (snake_case)
    }
)


def format_address_fields(data: Any) -> Any:
    """Recursively format address-bearing int fields as hex strings.

    Walks dicts and lists, converting any int value whose key is in
    ``ADDRESS_FIELD_NAMES`` to a hex string via ``format_address``.
    Non-address ints (sizes, counts, priorities) are left unchanged.
    Already-string address fields are left unchanged (idempotent).

    This is the single place that normalizes PPSSPP's raw WS responses
    (which return addresses as decimal ints, e.g. ``{"pc": 143585524}``)
    into the hex-string convention used everywhere else in this codebase
    (``{"pc": "0x088ED0F4"}``), so the Agent never sees mixed formats.

    Args:
        data: PPSSPP raw response (dict / list / scalar).

    Returns:
        Same shape as ``data`` with address int fields replaced by
        hex strings. Non-address fields are untouched. The input is
        not mutated — a shallow copy is made at each dict level.

    Examples:
        >>> format_address_fields({"pc": 143585524, "name": "user_main"})
        {'pc': '0x088ED0F4', 'name': 'user_main'}
        >>> format_address_fields([{"address": 142622720, "size": 824}])
        [{'address': '0x08804000', 'size': 824}]
    """
    if isinstance(data, dict):
        result: dict[str, Any] = {}
        for key, val in data.items():
            if key in ADDRESS_FIELD_NAMES and isinstance(val, int) and not isinstance(val, bool):
                result[key] = format_address(val)
            else:
                result[key] = format_address_fields(val)
        return result
    if isinstance(data, list):
        return [format_address_fields(item) for item in data]
    return data
