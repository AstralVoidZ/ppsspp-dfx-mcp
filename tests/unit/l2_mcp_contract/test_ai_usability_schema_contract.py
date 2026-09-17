"""L2 MCP contract: AI-usability schema surfacing (analysis_mcp_ai_usability_v1).

Locks three schema-level guarantees from the AI-usability round:

- ppsspp_state_observer register size is a schema enum (1/2/4) matching
  the runtime ``_VALID_SIZES`` check — previously the constraint existed
  only at runtime, so models could generate invalid widths freely.
- ppsspp_batch_step steps is a discriminated union: per-type properties
  plus a 'type' discriminator — previously an untyped ``list[dict]``
  with zero generation constraints.
- ppsspp_breakpoint mem size stays an int (range watches are legitimate)
  but its description must document fixed-width vs range semantics.
"""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp import server as server_mod
from ppsspp_dfx_mcp.models.input import PPSSPP_ALL_BUTTONS

pytestmark = pytest.mark.asyncio


async def _tool_schema(name: str) -> dict:
    server_mod.register_all_tools()
    tools = await server_mod.mcp.list_tools()
    tool = next(t for t in tools if t.name == name)
    return tool.input_schema


class TestBatchStepsDiscriminatedUnion:
    async def test_steps_have_type_discriminator(self) -> None:
        """steps items carry a 'type' discriminator with the 4 step types."""
        schema = await _tool_schema("ppsspp_batch_step")
        items = schema["properties"]["steps"]["items"]
        disc = items.get("discriminator", {})
        assert disc.get("propertyName") == "type", (
            "batch steps must be a discriminated union on 'type' — "
            "untyped dict list regression"
        )
        assert set(disc.get("mapping", {})) == {
            "press", "wait", "state_probe", "screenshot",
        }

    async def test_step_defs_have_typed_fields(self) -> None:
        """Per-type $defs carry typed properties (button/frames/...)."""
        schema = await _tool_schema("ppsspp_batch_step")
        defs = schema.get("$defs", {})
        button = defs.get("PressStep", {}).get("properties", {}).get("button", {})
        assert "button" in defs.get("PressStep", {}).get("properties", {})
        assert button.get("enum") == list(PPSSPP_ALL_BUTTONS), (
            "PressStep.button must surface the 25-item whitelist as a "
            "schema enum — a bare string regresses generation guidance"
        )
        assert "frames" in defs.get("WaitStep", {}).get("properties", {})
        assert "samples" in defs.get("StateProbeStep", {}).get("properties", {})
        assert "source" in defs.get("ScreenshotStep", {}).get("properties", {})


class TestStateObserverSizeEnum:
    async def test_register_size_is_schema_enum(self) -> None:
        """size surfaces as enum [1, 2, 4] — matches runtime _VALID_SIZES."""
        schema = await _tool_schema("ppsspp_state_observer")
        size = schema["properties"]["size"]
        assert sorted(size.get("enum", [])) == [1, 2, 4], (
            "state_observer size must be a schema enum (1/2/4), not a "
            "free int — the runtime check alone lets models generate "
            "invalid widths"
        )


class TestBreakpointSizeSemantics:
    async def test_mem_size_documents_fixed_vs_range(self) -> None:
        """size stays int (range watches legit) but the description must
        state the fixed-width 1/2/4 and range-watch distinction."""
        schema = await _tool_schema("ppsspp_breakpoint")
        desc = schema["properties"]["size"]["description"]
        assert "1/2/4" in desc and "range" in desc, (
            "breakpoint size description must document fixed-width vs "
            "range-watch semantics"
        )
