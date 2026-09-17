"""R2 (design_ppsspp_dfx_mcp_test_refactor_v1 §R2): schema ↔ runtime enum
consistency.

For every aggregate tool whose ``action`` parameter drives a runtime
dispatch, the Literal enum baked into the MCP inputSchema MUST equal the
runtime ``_*_ACTIONS`` tuple used for dispatch and error messages.

Regression anchor: verification report F-2 — the ``register`` action of
``ppsspp_query`` existed in the runtime dispatch (and unit tests called
the function directly) but was missing from the schema Literal, making
the capability UNREACHABLE through real MCP clients while 1106 unit
tests stayed green.
"""

from __future__ import annotations

import importlib

import pytest

from ppsspp_dfx_mcp import server as server_mod

pytestmark = pytest.mark.asyncio

# (tool name, action parameter name, module path, runtime constant name)
AGGREGATE_TOOLS: list[tuple[str, str, str, str]] = [
    ("ppsspp_query", "action", "ppsspp_dfx_mcp.tools.query", "_QUERY_ACTIONS"),
    ("ppsspp_read_memory", "action", "ppsspp_dfx_mcp.tools.memory", "_READ_ACTIONS"),
    ("ppsspp_step", "action", "ppsspp_dfx_mcp.tools.step", "_STEP_ACTIONS"),
    ("ppsspp_breakpoint", "action", "ppsspp_dfx_mcp.tools.breakpoint", "_BP_ACTIONS"),
    ("ppsspp_replay", "action", "ppsspp_dfx_mcp.tools.replay", "_REPLAY_ACTIONS"),
    ("ppsspp_state_observer", "action", "ppsspp_dfx_mcp.tools.state_observer", "_ACTIONS"),
]

server_mod.register_all_tools()


def _schema_action_enum(tool_name: str, param: str = "action") -> set[str]:
    tool = server_mod.mcp._tool_manager._tools[tool_name]
    schema = tool.parameters or {}
    enum = (schema.get("properties", {}).get(param, {}) or {}).get("enum")
    assert enum is not None, (
        f"{tool_name}.{param} has no enum in its inputSchema — the Literal "
        f"was replaced by an unbounded type, breaking schema-level discovery"
    )
    return set(enum)


def _runtime_actions(module_path: str, constant: str) -> set[str]:
    mod = importlib.import_module(module_path)
    return set(getattr(mod, constant))


class TestSchemaRuntimeEnumConsistency:
    """inputSchema enum must equal the runtime dispatch set, both ways."""

    @pytest.mark.parametrize(("tool", "param", "module_path", "constant"), AGGREGATE_TOOLS)
    async def test_schema_enum_equals_runtime_actions(
        self, tool: str, param: str, module_path: str, constant: str
    ) -> None:
        schema_enum = _schema_action_enum(tool, param)
        runtime = _runtime_actions(module_path, constant)
        missing_in_schema = runtime - schema_enum
        assert not missing_in_schema, (
            f"{tool}.{param}: runtime actions {sorted(missing_in_schema)} "
            f"are missing from the schema Literal — they are UNREACHABLE "
            f"via real MCP clients (F-2 regression)"
        )
        missing_in_runtime = schema_enum - runtime
        assert not missing_in_runtime, (
            f"{tool}.{param}: schema advertises {sorted(missing_in_runtime)} "
            f"but runtime dispatch rejects them (client-facing lie)"
        )
