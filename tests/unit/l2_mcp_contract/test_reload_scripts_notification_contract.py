"""test_reload_scripts_notification_contract.py — L2 契约：工具集变更告知。

Anchor: openspec change `ppsspp-dfx-mcp-protocol-and-schema`
- `specs/ppsspp-dfx-mcp-script-manifest/spec.md`（MODIFIED `ppsspp_reload_scripts 工具`）
- `design.md` D5 + D8

**为什么返回值是契约的核心**：SDK 2.2.0 的 `MCPServer` 未暴露握手时代的
`notification_options`，`capabilities.tools.list_changed` 恒为 `false`
（证据链见 design D8）。因此 agent 感知「工具集变了，需要重新 `tools/list`」
**唯一可靠**的通道是 `ppsspp_reload_scripts` 的返回值。

契约：
1. 工具集**实际增删**时，返回值中 `exposed_added` / `exposed_removed` 非空。
2. 工具集未变化时（幂等重载），两者均为**空数组**（`idempotentHint=True`）。
3. 集合变化时**尝试**发送 `notifications/tools/list_changed`——前瞻性措施，
   不作为 agent 的依赖路径。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


class _FakeManifest:
    """最小 manifest 替身：空注册表，使测试聚焦于告知契约而非条目构造。"""

    def __init__(self, tmp_path: Path):
        self._path = tmp_path / "scripts.manifest.yaml"
        self._path.write_text("scripts: []\n", encoding="utf-8")

    def reload(self) -> int:
        return 0

    def list_scripts(self) -> list:
        return []

    def manifest_path(self) -> Path:
        return self._path


def _report(added=(), removed=(), registered: int = 0) -> dict:
    """`sync_exposed_tools()` 的返回值形状（server.py:439-447）。"""
    return {
        "declared": 0,
        "registered": registered,
        "added": list(added),
        "removed": list(removed),
        "skipped_skeleton": [],
        "failed": [],
        "restart_required": False,
    }


@pytest.fixture
def patched_manifest(tmp_path: Path):
    """隔离 manifest 与模块缓存，使 `reload_scripts` 可脱离真实文件系统测试。"""
    manifest = _FakeManifest(tmp_path)
    with (
        patch("ppsspp_dfx_mcp.tools.script.get_manifest", return_value=manifest),
        patch("ppsspp_dfx_mcp.tools.script._clear_module_cache"),
        patch("ppsspp_dfx_mcp.server.registered_exposed_names", return_value=set()),
    ):
        yield manifest


def _ctx_with_session() -> SimpleNamespace:
    """构造只带 `request_context.session` 的最小 Context 替身。"""
    session = SimpleNamespace(send_tool_list_changed=AsyncMock())
    return SimpleNamespace(request_context=SimpleNamespace(session=session))


class _NoRequestContext:
    """Context 替身：访问 `request_context` 即抛错。

    复刻 SDK 的非请求路径——`MCPServer.call_tool(name, args)` 构造的 Context
    没有请求上下文（`mcpserver/server.py:540`），`Context.request_context`
    会抛 `ValueError("Context is not available outside of a request")`。
    """

    @property
    def request_context(self):  # noqa: ANN201
        raise ValueError("Context is not available outside of a request")


# ============================================================================
# 返回值契约（Agent 依赖的通道）
# ============================================================================


class TestToolSetChangeReportedInReturnValue:
    """返回值是 Agent 感知工具集变化的唯一可靠通道（design D8）。"""

    async def test_added_tool_surfaces(self, patched_manifest):
        from ppsspp_dfx_mcp.tools.script import reload_scripts

        with patch(
            "ppsspp_dfx_mcp.server.sync_exposed_tools",
            new=AsyncMock(return_value=_report(added=["fresh_script"], registered=1)),
        ):
            out = await reload_scripts()

        assert out["exposed_added"] == ["fresh_script"]
        assert out["exposed_removed"] == []

    async def test_removed_tool_surfaces(self, patched_manifest):
        from ppsspp_dfx_mcp.tools.script import reload_scripts

        with patch(
            "ppsspp_dfx_mcp.server.sync_exposed_tools",
            new=AsyncMock(return_value=_report(removed=["stale_script"], registered=0)),
        ):
            out = await reload_scripts()

        assert out["exposed_removed"] == ["stale_script"]
        assert out["exposed_added"] == []

    async def test_noop_reload_reports_empty_arrays(self, patched_manifest):
        """幂等重载：集合未变时两个字段均为空数组，Agent 据此判定无需重取列表。"""
        from ppsspp_dfx_mcp.tools.script import reload_scripts

        with patch(
            "ppsspp_dfx_mcp.server.sync_exposed_tools", new=AsyncMock(return_value=_report())
        ):
            out = await reload_scripts()

        assert out["exposed_added"] == []
        assert out["exposed_removed"] == []


# ============================================================================
# 通知（前瞻性措施，非依赖路径）
# ============================================================================


class TestToolListChangedNotification:
    """`notifications/tools/list_changed` 在 SDK 支持时生效，但不得被依赖（design D8）。"""

    async def test_sent_when_tool_set_changes(self, patched_manifest):
        from ppsspp_dfx_mcp.tools.script import reload_scripts

        ctx = _ctx_with_session()
        with patch(
            "ppsspp_dfx_mcp.server.sync_exposed_tools",
            return_value=_report(added=["a"], registered=1),
        ):
            await reload_scripts(ctx=ctx)

        ctx.request_context.session.send_tool_list_changed.assert_awaited_once()

    async def test_not_sent_on_noop_reload(self, patched_manifest):
        """幂等重载必须无副作用——不发通知（与 `idempotentHint=True` 契约一致）。"""
        from ppsspp_dfx_mcp.tools.script import reload_scripts

        ctx = _ctx_with_session()
        with patch(
            "ppsspp_dfx_mcp.server.sync_exposed_tools", new=AsyncMock(return_value=_report())
        ):
            await reload_scripts(ctx=ctx)

        ctx.request_context.session.send_tool_list_changed.assert_not_awaited()

    async def test_removal_also_notifies(self, patched_manifest):
        from ppsspp_dfx_mcp.tools.script import reload_scripts

        ctx = _ctx_with_session()
        with patch(
            "ppsspp_dfx_mcp.server.sync_exposed_tools", return_value=_report(removed=["gone"])
        ):
            await reload_scripts(ctx=ctx)

        ctx.request_context.session.send_tool_list_changed.assert_awaited_once()

    async def test_missing_ctx_does_not_raise(self, patched_manifest):
        """`ctx` 缺省（直调 / 测试路径）时不得抛错——工具仍须返回完整结果。"""
        from ppsspp_dfx_mcp.tools.script import reload_scripts

        with patch(
            "ppsspp_dfx_mcp.server.sync_exposed_tools",
            return_value=_report(added=["a"], registered=1),
        ):
            out = await reload_scripts()  # 不传 ctx

        assert out["exposed_added"] == ["a"]

    async def test_notification_failure_does_not_fail_the_reload(self, patched_manifest):
        """通知是**尽力而为的额外项**，任何失败都不得让重载报错。

        复刻真实场景：`MCPServer.call_tool()` 构造的 Context 无请求上下文，
        `ctx.request_context` 直接抛 ValueError。此时工具集**已经**被
        `sync_exposed_tools()` 改掉了——若让异常冒泡，调用方会收到"重载失败"
        而实际已生效，并且无法从返回值得知变化内容（那是 Agent 唯一的通道）。
        """
        from ppsspp_dfx_mcp.tools.script import reload_scripts

        with patch(
            "ppsspp_dfx_mcp.server.sync_exposed_tools",
            return_value=_report(added=["fresh"], registered=1),
        ):
            out = await reload_scripts(ctx=_NoRequestContext())

        assert out["exposed_added"] == ["fresh"]

    async def test_transport_failure_does_not_fail_the_reload(self, patched_manifest):
        """WS/传输层报错同样不得冒泡。"""
        from ppsspp_dfx_mcp.tools.script import reload_scripts

        ctx = _ctx_with_session()
        ctx.request_context.session.send_tool_list_changed.side_effect = OSError("transport gone")
        with patch(
            "ppsspp_dfx_mcp.server.sync_exposed_tools",
            return_value=_report(added=["fresh"], registered=1),
        ):
            out = await reload_scripts(ctx=ctx)

        assert out["exposed_added"] == ["fresh"]
