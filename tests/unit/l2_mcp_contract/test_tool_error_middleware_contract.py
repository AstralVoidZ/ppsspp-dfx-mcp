"""L2 contract: pre-validation middleware normalizes args errors (FR-002/G-2).

`tool_error_middleware` (middleware.py) is the single point that turns a
pydantic `ValidationError` from the SDK's own argument validation into
`[ARGS_INVALID] <field>: <msg>`. These are unit-level checks of the
middleware's decision table + a drift guard on the private SDK API it uses.

The real-wire behaviour (JSON-RPC `isError:true`) is covered end-to-end by
`tests/mcp_inspector/test_args_validation.py`; this file locks the internal
contract so a refactor that breaks the short-circuit is caught cheaply.
"""

from __future__ import annotations

import pytest
from mcp.types import CallToolResult
from pydantic import BaseModel, ValidationError

from ppsspp_dfx_mcp import server as server_mod
from ppsspp_dfx_mcp.middleware import _format_args_invalid, tool_error_middleware


class _Ctx:
    """Minimal stand-in for the SDK's ServerRequestContext."""

    def __init__(self, method: str, params: object) -> None:
        self.method = method
        self.params = params


class _RecordCallNext:
    """call_next that records invocation and returns a sentinel."""

    def __init__(self) -> None:
        self.called = False
        self.sentinel = object()

    async def __call__(self, ctx: object) -> object:
        self.called = True
        return self.sentinel


# ============================================================================
# _format_args_invalid
# ============================================================================


class TestFormatArgsInvalid:
    def test_single_field_message(self):
        class M(BaseModel):
            session_id: str

        try:
            M.model_validate({"session_id": 123})
        except ValidationError as exc:
            text = _format_args_invalid(exc)
        assert text.startswith("[ARGS_INVALID] ")
        assert "session_id:" in text
        # Never leak pydantic internals.
        assert "validation error for" not in text
        assert "[type=" not in text
        assert "errors.pydantic.dev" not in text
        assert "Arguments" not in text

    def test_root_level_error_uses_root_placeholder(self):
        class M(BaseModel):
            x: int

        try:
            M.model_validate(123)
        except ValidationError as exc:
            text = _format_args_invalid(exc)
        assert "(root):" in text


# ============================================================================
# tool_error_middleware decision table
# ============================================================================


class TestMiddlewareDecisionTable:
    @pytest.mark.asyncio
    async def test_short_circuits_on_validation_error(self):
        server_mod.register_all_tools()
        ctx = _Ctx(
            "tools/call",
            {"name": "ppsspp_breakpoint", "arguments": {"session_id": 123}},
        )
        nxt = _RecordCallNext()
        result = await tool_error_middleware(ctx, nxt)
        assert isinstance(result, CallToolResult)
        assert result.is_error is True
        assert "[ARGS_INVALID]" in result.content[0].text
        assert nxt.called is False, "must short-circuit before the SDK validates"

    @pytest.mark.asyncio
    async def test_valid_args_pass_through(self):
        server_mod.register_all_tools()
        ctx = _Ctx(
            "tools/call",
            {"name": "ppsspp_health", "arguments": {}},
        )
        nxt = _RecordCallNext()
        result = await tool_error_middleware(ctx, nxt)
        assert nxt.called is True
        assert result is nxt.sentinel

    @pytest.mark.asyncio
    async def test_unknown_tool_passes_through(self):
        ctx = _Ctx("tools/call", {"name": "ppsspp_not_a_tool", "arguments": {}})
        nxt = _RecordCallNext()
        result = await tool_error_middleware(ctx, nxt)
        assert nxt.called is True
        assert result is nxt.sentinel

    @pytest.mark.asyncio
    async def test_non_tools_call_method_passes_through(self):
        ctx = _Ctx("tools/list", {"name": "ppsspp_breakpoint"})
        nxt = _RecordCallNext()
        result = await tool_error_middleware(ctx, nxt)
        assert nxt.called is True
        assert result is nxt.sentinel

    @pytest.mark.asyncio
    async def test_missing_arguments_treated_as_empty(self):
        """A no-arg-shaped call still validates (missing required → error)."""
        server_mod.register_all_tools()
        ctx = _Ctx("tools/call", {"name": "ppsspp_breakpoint"})
        nxt = _RecordCallNext()
        result = await tool_error_middleware(ctx, nxt)
        assert isinstance(result, CallToolResult)
        assert result.is_error is True
        assert "[ARGS_INVALID]" in result.content[0].text
        assert nxt.called is False


# ============================================================================
# SDK drift guard
# ============================================================================


class TestSdkDriftGuard:
    """The middleware depends on private SDK internals; a drift must be loud.

    If a future SDK renames `fn_metadata.validate_arguments` or stops
    exposing `_tool_manager._tools`, the middleware silently degrades to
    pass-through (no validation). These assertions fail first.
    """

    def test_tool_manager_private_registry_present(self):
        server_mod.register_all_tools()
        tools = server_mod.mcp._tool_manager._tools
        assert tools is not None and len(tools) > 0

    def test_every_tool_exposes_validate_arguments(self):
        server_mod.register_all_tools()
        for name, tool in server_mod.mcp._tool_manager._tools.items():
            md = getattr(tool, "fn_metadata", None)
            assert md is not None, f"{name}: no fn_metadata"
            assert callable(getattr(md, "validate_arguments", None)), (
                f"{name}: fn_metadata.validate_arguments missing"
            )
