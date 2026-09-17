"""List known address constants from addresses.yaml.

1 tool exposed:
- ppsspp_list_addresses(section?) — return all known address constants
  from `.ppsspp-dfx/config/addresses.yaml`, with every address value
  formatted as a hex string (e.g. "0x08804000").

Why this tool exists:
- Before this tool, the address constants maintained in addresses.yaml
  (game_mode_addr, known_functions, state_probes, etc.) were invisible
  to the MCP client. The Agent had to rely on memory or user prompts to
  know addresses, which caused repeated "wrong address" failures.
- Exposing the constants lets the Agent query "what is the address of
  game_mode / sub_DC7B4 / ..." before calling read_memory / breakpoint,
  eliminating guesswork.

Return format:
- All int address values are formatted as hex strings ("0x%08X"), so the
  Agent can pass them directly to other tools (which now all accept hex
  strings).
- Non-address int values (e.g. sizes, offsets) are kept as-is or
  formatted as hex if they look like addresses.
- Lists (e.g. vram_candidates) are formatted element-by-element.

READ-ONLY: does not contact PPSSPP. Pure YAML read + format.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, TypedDict

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp import config
from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.tools._common import translate_tool_errors

logger = logging.getLogger(__name__)

__all__ = ["list_addresses"]


class ListAddressesOutput(TypedDict):
    """`ppsspp_list_addresses` 的结构化返回契约（openspec `tool-schema-contract`）。

    本工具直接构造 dict 字面量、**没有对应的 Pydantic view**，故此处手工声明
    而非用 `views/_contract.derive_output_contract` 派生（其余 32 个工具均为派生）。
    字段与下文的两处 `return {...}` 字面量保持一致。
    """

    sections: dict[str, Any]
    count: int
    section_filter: str | None


def _format_value(v: Any) -> Any:
    """Recursively format int values as hex strings.

    Ints that look like addresses (>= 0x1000) are formatted via
    format_address (padded to 8 hex digits, uppercase, "0x" prefix).
    Small ints (sizes, offsets < 0x1000) are kept as decimal int — they
    are not addresses and hex formatting would confuse.
    """
    if isinstance(v, int):
        if v >= 0x1000:
            return format_address(v)
        return v
    if isinstance(v, list):
        return [_format_value(x) for x in v]
    if isinstance(v, dict):
        return {k: _format_value(val) for k, val in v.items()}
    return v


# Former docstring (kept as comment; description is now the TDQS docstring):
# List known address constants from addresses.yaml.
#
# Reads `.ppsspp-dfx/config/addresses.yaml` and returns all entries.
# Every address value is formatted as a hex string (e.g.
# "0x08804000") so it can be passed directly to ppsspp_read_memory,
# ppsspp_breakpoint, etc.
#
# Args:
# section: Optional section name to filter (e.g.
# "known_functions" returns only the function address table).
#
# Returns:
# Dict mapping section names to their contents. If `section` is
# specified, only that section is returned. If the section does
# not exist, an empty dict is returned.
@mcp.tool(
    name="ppsspp_list_addresses",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
@translate_tool_errors
async def list_addresses(
    section: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional section filter (e.g. 'known_functions', "
                "'state_probes', 'top_base'). If omitted, returns all "
                "sections."
            ),
        ),
    ] = None,
) -> ListAddressesOutput:
    """PURPOSE: List the project's known address constants from addresses.yaml — the single source of truth; never guess hex addresses.

    USAGE: optional section filter; an unknown section returns an error listing the valid ones.


    CONVERSION: IDA <-> PPSSPP address conversion is plain arithmetic — ppsspp_addr = ida_addr + (top_base.ppsspp - top_base.ida) (defaults 0x08804000 - 0x00000000). ppsspp_convert_address was un-tooled in v0.1.6.
    BEHAVIOR: READ-ONLY. Int values ≥0x1000 are returned as hex strings that can be pasted straight into address parameters.

    RETURNS: {sections, count, section_filter}."""
    try:
        addrs = config.addresses()
    except Exception as e:
        logger.warning("list_addresses: failed to load addresses.yaml: %s", e)
        return {"sections": {}, "count": 0, "section_filter": section}

    if section is not None:
        if section in addrs:
            result = {section: _format_value(addrs[section])}
        else:
            # An unknown section is a caller mistake
            # (typically a typo) — fail with the valid sections instead of
            # silently returning an empty result.
            raise ArgsInvalid(
                f"unknown section {section!r}; valid sections: "
                f"{sorted(k for k in addrs if isinstance(k, str))}"
            )
        return {
            "sections": result,
            "count": len(result),
            "section_filter": section,
        }

    formatted = _format_value(addrs)
    return {
        "sections": formatted,
        "count": len(formatted),
        "section_filter": None,
    }
