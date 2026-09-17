"""Wire-level checks for the protocol/schema change (task 6.5).

Unlike the L2 contract tests — which import the server in-process and
inspect its objects — these run against a **real server subprocess over
stdio**, i.e. exactly what MCP Inspector and every other MCP client sees.
That distinction matters for the claims this change makes:

- "`completions` is declared" must hold on the wire, not just on the
  in-process `ServerCapabilities` model.
- "`dump_clut` no longer trips the client's schema linter" is about the
  JSON actually transmitted; the Inspector warning that started this
  change came from validating that JSON, not from our Python objects.
- The completion round-trip proves a client can *use* the new handler
  (signature, ref routing, serialization) rather than that our function
  returns the right value when called directly.

Tests share the session-scoped `mcp_inspector` fixture (loop_scope must
match, per its docstring).
"""

from __future__ import annotations

import pytest
from mcp.types import PromptReference

_ASYNC = pytest.mark.asyncio(loop_scope="session")


@_ASYNC
class TestCapabilitiesOnTheWire:
    """Handshake capabilities as a client sees them.

    Uses the public `ClientSession.server_capabilities` accessor rather
    than the session's private init result — that is what a real client
    (Inspector included) reads off the handshake.
    """

    async def test_completions_declared(self, mcp_inspector):
        caps = mcp_inspector.server_capabilities
        assert caps.completions is not None

    async def test_tools_resources_prompts_declared_on_wire(self, mcp_inspector):
        caps = mcp_inspector.server_capabilities
        assert caps.tools is not None
        assert caps.resources is not None
        assert caps.prompts is not None

    async def test_unreachable_capabilities_not_faked(self, mcp_inspector):
        """design D8: 不可达项不得伪造声明（详见 L2 capabilities 契约测试）。"""
        caps = mcp_inspector.server_capabilities
        assert caps.tools.list_changed is False
        assert caps.resources.subscribe is False

    async def test_logging_and_tasks_absent(self, mcp_inspector):
        caps = mcp_inspector.server_capabilities
        caps_dict = caps.model_dump(exclude_none=True)
        assert "logging" not in caps_dict
        assert "tasks" not in caps_dict


@_ASYNC
class TestOutputSchemasOnTheWire:
    """`outputSchema` 的形态即客户端 schema 校验器的输入。"""

    async def _schema(self, session, tool_name: str) -> dict:
        tools = (await session.list_tools()).tools
        tool = next(t for t in tools if t.name == tool_name)
        assert tool.output_schema is not None, f"{tool_name} 无 outputSchema"
        return tool.output_schema

    async def test_dump_clut_has_no_unconstrained_items(self, mcp_inspector):
        """原始 issue：`items: {}` 触发 Inspector schema 告警。"""
        schema = await self._schema(mcp_inspector, "ppsspp_dump_clut")
        for prop in (schema.get("properties") or {}).values():
            assert prop.get("items") != {}, (
                "dump_clut 的数组返回仍未约束 items —— 即最初的 Inspector 告警形态"
            )

    @pytest.mark.parametrize(
        "tool_name",
        ["ppsspp_screenshot", "ppsspp_dump_texture", "ppsspp_dump_clut"],
    )
    async def test_image_tools_declare_metadata_fields(self, mcp_inspector, tool_name: str):
        """图像工具的元数据契约可见（此前 screenshot/dump_texture 完全无 schema）。"""
        schema = await self._schema(mcp_inspector, tool_name)
        props = schema.get("properties") or {}
        assert props, f"{tool_name} 的 outputSchema 无字段声明"
        assert "file_path" in props
        # 像素走 content，不进结构化通道。
        assert "image_base64" not in props

    async def test_dump_texture_metadata_has_no_phantom_fields(self, mcp_inspector):
        """spec 修正：address/texfmt/width/height 该协议不支持，不得出现在契约里。"""
        schema = await self._schema(mcp_inspector, "ppsspp_dump_texture")
        props = set(schema.get("properties") or {})
        assert not (props & {"address", "texfmt", "width", "height"}), props


@_ASYNC
class TestCompletionRoundTrip:
    """`completion/complete` 走真实 JSON-RPC 往返。"""

    async def test_address_completion_over_the_wire(self, mcp_inspector):
        result = await mcp_inspector.complete(
            PromptReference(name="memory-breakpoint-wizard"),
            {"name": "address", "value": "0x088"},
        )
        assert result.completion.values, "wire 往返应返回地址候选"
        for value in result.completion.values:
            assert value.lower().startswith("0x088"), value

    async def test_non_address_argument_returns_empty(self, mcp_inspector):
        result = await mcp_inspector.complete(
            PromptReference(name="memory-breakpoint-wizard"),
            {"name": "size", "value": "4"},
        )
        assert result.completion.values == []
