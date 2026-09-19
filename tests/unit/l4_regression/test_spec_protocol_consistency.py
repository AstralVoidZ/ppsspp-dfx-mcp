"""CI 校验：仓内协议描述（工具 docstring 源码）与 PPSSPP 协议契约一致。

防止历史描述错误重现（不做全量对照，只防已知问题）：
- E-1: memory-info-search 的描述曾错误说 "list"（应为 "extent"）
- E-2: gpu-record 的描述曾错误说 "CPU must be running"
- E-3: debug-client 的 memory_map 事件曾错误用 "memory.info.list"
  （应为 "memory.mapping"）

Anchor:
- 读取 src/ 下的工具/服务源码，对关键字符串做存在/缺失断言。工具
  docstring 会原样下发为 MCP 工具描述（Agent 的唯一协议依据），是本仓
  对应 openspec 时代 spec.md 的"仓内真源"。
- 如果描述被改回错误内容，对应断言立即失败。

（C4，review v2：旧版读取仓库外的 openspec/specs/，独立仓 CI 上整个
模块静默 skip——回归锁形同虚设。现改为仓内真源，CI 强制执行。）
"""

from __future__ import annotations

import pathlib

# 仓库根 = test 文件向上 4 级（l4_regression → unit → tests → 仓库根）。
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SRC = _REPO_ROOT / "src" / "ppsspp_dfx_mcp"


def _read_source(relpath: str) -> str:
    """读取 src/ppsspp_dfx_mcp/{relpath} 全文。"""
    path = _SRC / relpath
    assert path.exists(), f"源文件不存在: {path} — 模块被移动后请同步更新本测试。"
    return path.read_text(encoding="utf-8")


class TestSpecProtocolConsistency:
    """仓内协议描述必须与 PPSSPP 实际契约一致。"""

    # ==================================================================
    # E-1: search_memory_info — "extent" not "list"
    # ==================================================================

    def test_search_memory_info_doc_contains_extent(self):
        """E-1 回归：search_memory_info 描述应包含 "extent"。

        PPSSPP memory.info.search 返回单个 extent（null | object），
        不是列表。之前描述错误为 "list"，导致调用方写成
        result["regions"][0] 并在无匹配时静默失败。
        """
        source = _read_source("tools/search_memory_info.py")
        assert "extent" in source, (
            "search_memory_info 描述必须包含 'extent' — "
            "PPSSPP memory.info.search 返回 extent（null | 单对象），"
            "不是列表。如果此断言失败，E-1 回归（描述错误说 list）。"
        )

    def test_search_memory_info_doc_no_list_of_regions_phrase(self):
        """E-1 回归：不应包含 "Returns a list of matching memory regions"。"""
        source = _read_source("tools/search_memory_info.py")
        assert "Returns a list of matching memory regions" not in source, (
            "search_memory_info 描述不应包含 "
            "'Returns a list of matching memory regions' — "
            "PPSSPP 返回单个 extent，不是列表。E-1 回归。"
        )

    # ==================================================================
    # E-2: gpu_record — no "CPU must be running"
    # ==================================================================

    def test_gpu_record_doc_no_cpu_must_be_running(self):
        """E-2 回归：gpu_record 描述不应包含 "CPU must be running"。

        PPSSPP gpu.record.dump 只检查 PSP_IsInited()，不要求 CPU
        处于运行状态。CPU 暂停时调用仍可返回（但 dump 可能为空）。
        之前描述错误声明 CPU 必须运行，误导调用方。
        """
        source = _read_source("tools/gpu_record.py")
        assert "CPU must be running" not in source, (
            "gpu_record 描述不应包含 'CPU must be running' — "
            "PPSSPP gpu.record.dump 只检查 PSP_IsInited()，不要求 "
            "CPU 运行。E-2 回归。"
        )

    # ==================================================================
    # E-3: debug_client — "memory.mapping" not "memory.info.list"
    # ==================================================================

    def test_debug_client_contains_memory_mapping(self):
        """E-3 回归：debug_client 应包含 "memory.mapping"。

        PPSSPP MemoryInfoSubscriber 注册的是 memory.mapping 事件
        （用于地址空间列表），不是 memory.info.list。之前实现
        错误使用 memory.info.list 作为 memory_map() 的事件名。
        """
        source = _read_source("service/debug_client.py")
        assert "memory.mapping" in source, (
            "debug_client 必须包含 'memory.mapping' — "
            "PPSSPP 注册 memory.mapping 用于地址空间列表。"
            "如果此断言失败，E-3 回归（错误说 memory.info.list）。"
        )

    def test_debug_client_no_memory_info_list_as_event_name(self):
        """E-3 回归：不应将 "memory.info.list" 作为活动事件名。

        源码中 memory.info.list 可出现在历史注释或 "not X" 上下文中
        （如 `not "memory.info.list"`），但不应出现在 transport.call()
        中作为实际调用的事件名。当前实现使用
        `self._transport.call("memory.mapping")` — 正确。
        """
        source = _read_source("service/debug_client.py")
        # 精确匹配: 不应出现 transport.call("memory.info.list") 调用。
        # 这捕获 "将 memory.info.list 作为活动事件名" 的回归，
        # 同时允许注释中引用 memory.info.list（包括 "not X" 上下文）。
        assert 'transport.call("memory.info.list"' not in source, (
            "debug_client 不应在 transport.call() 中使用 "
            "'memory.info.list' 作为事件名 — PPSSPP 使用 memory.mapping。"
            "E-3 回归。注释中可引用 memory.info.list（包括 "
            "'not \"memory.info.list\"' 上下文），但实际调用应使用 "
            "memory.mapping。"
        )
        # 同时检查 WS event 声明形式不应使用 memory.info.list 作为活动事件
        assert 'WS event "memory.info.list"' not in source, (
            "debug_client 不应将 'memory.info.list' 声明为 "
            "WS event（活动事件名）— PPSSPP 使用 memory.mapping。"
            "E-3 回归。"
        )
