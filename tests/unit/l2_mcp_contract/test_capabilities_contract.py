"""test_capabilities_contract.py — L2 契约：capabilities 声明与实现一致。

Anchor: openspec change `ppsspp-dfx-mcp-protocol-and-schema`
- `specs/ppsspp-dfx-mcp-server/spec.md`（ADDED `server SHALL 声明与实际实现一致的 capabilities`）
- `design.md` D8（SDK 2.2.0 不可达项）

契约：
1. 已注册 handler 的能力 SHALL 出现（`tools` / `resources` / `prompts`）。
2. 未实现的能力 SHALL NOT 出现（`logging`——协议 2026 修订已移除 `logging/setLevel`）。
3. **SDK 不可达的能力 SHALL NOT 被伪造**：`tools.list_changed` 与
   `resources.subscribe` 必须为 `false`。这不是"还没做"，而是 SDK 2.2.0 的
   `MCPServer` 未提供握手时代的 `notification_options` 入口（证据链见 design D8）。
   若将来 SDK 开放该入口，本测试会失败——那是**需要更新设计**的信号，而非回归。
"""

from __future__ import annotations


def _capabilities() -> dict:
    """取 server 在握手时下发的 capabilities。

    `MCPServer` 未公开该访问路径（`create_initialization_options` 挂在
    `_lowlevel_server` 上），但 `MCPServer` 自己在 stdio / streamable-http
    两条启动路径上就是这么调的（`mcpserver/server.py:1071,1171`）。故此处
    复现该调用——断言的是**握手真实产物**，不是内部状态。
    """
    from ppsspp_dfx_mcp.server import mcp

    options = mcp._lowlevel_server.create_initialization_options()
    return options.capabilities.model_dump(exclude_none=True)


class TestDeclaredCapabilities:
    """已实现的能力必须在握手中声明。"""

    def test_tools_resources_prompts_declared(self):
        caps = _capabilities()
        assert "tools" in caps
        assert "resources" in caps
        assert "prompts" in caps

    def test_instructions_present(self):
        """server instructions 是 Agent 的引导通道（design D8 的替代路径之一）。"""
        from ppsspp_dfx_mcp.server import mcp

        options = mcp._lowlevel_server.create_initialization_options()
        assert options.instructions, "instructions 不得为空——它承载工具集变更的引导语"


class TestUnimplementedCapabilities:
    """未实现的能力不得声明。"""

    def test_logging_not_declared(self):
        """Logging 未注册 handler（且协议 2026-07-28 已移除该能力）。"""
        assert "logging" not in _capabilities()

    def test_tasks_not_declared(self):
        """Tasks 在 SDK 2.2.0 中只有类型定义、无服务端实现。"""
        assert "tasks" not in _capabilities()


class TestSdkUnreachableCapabilitiesNotFaked:
    """design D8：SDK 不可达的能力不得伪造声明。

    这组断言是**变更意图的固化**——它们把「为什么没有 list_changed」写成了
    可执行的契约，防止后人误以为遗漏而"补上"一个客户端会误解的声明。
    """

    def test_tools_list_changed_is_false(self):
        """`MCPServer` 不透传 `notification_options`，故恒为 false。"""
        caps = _capabilities()
        assert caps["tools"]["list_changed"] is False

    def test_resources_subscribe_is_false(self):
        """`mcpserver/resources/` 无 subscribe handler 注册途径，故恒为 false。"""
        caps = _capabilities()
        assert caps["resources"]["subscribe"] is False

    def test_prompts_list_changed_is_false(self):
        """同一机制：prompts 的 list_changed 同样不可置位。"""
        caps = _capabilities()
        assert caps["prompts"]["list_changed"] is False


class TestBuiltinMiddlewareChain:
    """SDK 内置中间件不得被重复挂载（design D4）。

    这组断言固化「不自行挂载 OTel」的结论。若将来 SDK 移除该内置，本测试会
    失败——那是**需要更新设计**（改为自行挂载）的信号，而非回归。
    """

    def test_opentelemetry_middleware_appears_exactly_once(self):
        """`MCPServer` 已内置 OTel，且用户 middleware 跑在其内部。

        自行挂载会让每个入站消息产生**重复 span**（实施期实测）——当时链为
        `[OTel, RequestStateBoundary, request_id, rate_limit, OTel]`。
        """
        from ppsspp_dfx_mcp.server import mcp

        names = [type(m).__name__ for m in mcp._lowlevel_server.middleware]
        assert names.count("OpenTelemetryMiddleware") == 1, (
            f"OTel 中间件应恰好出现一次（SDK 内置），实际链：{names}"
        )

    def test_sdk_builtins_are_outermost(self):
        """SDK built-in 包在外层，用户 middleware 在内——顺序不可颠倒。"""
        from ppsspp_dfx_mcp.server import mcp

        names = [type(m).__name__ for m in mcp._lowlevel_server.middleware]
        assert names[0] == "OpenTelemetryMiddleware", names

    def test_project_middlewares_still_present(self):
        """既有 RequestId / RateLimit 中间件不得因本次变更丢失。"""
        from ppsspp_dfx_mcp.server import mcp

        names = [getattr(m, "__name__", type(m).__name__) for m in mcp._lowlevel_server.middleware]
        assert "request_id_middleware" in names, names
        assert "rate_limit_middleware" in names, names
