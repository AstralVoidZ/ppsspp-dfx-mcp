"""CI 校验：spec 文件中的返回字段描述与 PPSSPP 协议契约一致。

防止历史 spec 错误重现（不做全量对照，只防已知问题）：
- E-1: memory-info-search-tool spec 曾错误说 "list"（应为 "extent"）
- E-2: gpu-record-tool spec 曾错误说 "CPU must be running"
- E-3: debug-client spec 曾错误用 "memory.info.list" 作为事件名

Anchor:
- 读取 openspec/specs/ 下的 spec.md，对关键字符串做存在/缺失断言。
- 如果 spec 被改回错误描述，对应断言立即失败。
"""

from __future__ import annotations

import pathlib

import pytest

# 项目根目录 = test 文件向上 5 级（l4_regression → unit → tests → ppsspp-dfx-mcp → mcps → 仓库根）
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[5]
_SPECS_DIR = _PROJECT_ROOT / "openspec" / "specs"

pytestmark = pytest.mark.skipif(
    not _SPECS_DIR.is_dir(),
    reason="openspec/specs not available (standalone package checkout)",
)


def _read_spec(tool_name: str) -> str:
    """读取 openspec/specs/{tool_name}/spec.md 全文。"""
    spec_path = _SPECS_DIR / tool_name / "spec.md"
    assert spec_path.exists(), (
        f"spec 文件不存在: {spec_path} — 确认 openspec/specs/{tool_name}/spec.md 已创建。"
    )
    return spec_path.read_text(encoding="utf-8")


class TestSpecProtocolConsistency:
    """Spec 文件中的协议描述必须与 PPSSPP 实际契约一致。"""

    # ==================================================================
    # E-1: memory-info-search-tool — "extent" not "list"
    # ==================================================================

    def test_search_memory_info_spec_contains_extent(self):
        """E-1 回归：memory-info-search-tool spec 应包含 "extent"。

        PPSSPP memory.info.search 返回单个 extent（null | object），
        不是列表。之前 spec 错误描述为 "list"，导致调用方写成
        result["regions"][0] 并在无匹配时静默失败。
        """
        spec = _read_spec("memory-info-search-tool")
        assert "extent" in spec, (
            "memory-info-search-tool spec 必须包含 'extent' — "
            "PPSSPP memory.info.search 返回 extent（null | 单对象），"
            "不是列表。如果此断言失败，E-1 回归（spec 错误说 list）。"
        )

    def test_search_memory_info_spec_no_list_of_regions_phrase(self):
        """E-1 回归：不应包含 "Returns a list of matching memory regions"。"""
        spec = _read_spec("memory-info-search-tool")
        assert "Returns a list of matching memory regions" not in spec, (
            "memory-info-search-tool spec 不应包含 "
            "'Returns a list of matching memory regions' — "
            "PPSSPP 返回单个 extent，不是列表。E-1 回归。"
        )

    # ==================================================================
    # E-2: gpu-record-tool — no "CPU must be running"
    # ==================================================================

    def test_gpu_record_spec_no_cpu_must_be_running(self):
        """E-2 回归：gpu-record-tool spec 不应包含 "CPU must be running"。

        PPSSPP gpu.record.dump 只检查 PSP_IsInited()，不要求 CPU
        处于运行状态。CPU 暂停时调用仍可返回（但 dump 可能为空）。
        之前 spec 错误声明 CPU 必须运行，误导调用方。
        """
        spec = _read_spec("gpu-record-tool")
        assert "CPU must be running" not in spec, (
            "gpu-record-tool spec 不应包含 'CPU must be running' — "
            "PPSSPP gpu.record.dump 只检查 PSP_IsInited()，不要求 "
            "CPU 运行。E-2 回归。"
        )

    # ==================================================================
    # E-3: debug-client — "memory.mapping" not "memory.info.list"
    # ==================================================================

    def test_debug_client_spec_contains_memory_mapping(self):
        """E-3 回归：debug-client spec 应包含 "memory.mapping"。

        PPSSPP MemoryInfoSubscriber 注册的是 memory.mapping 事件
        （用于地址空间列表），不是 memory.info.list。之前 spec
        错误使用 memory.info.list 作为 memory_map() 的事件名。
        """
        spec = _read_spec("debug-client")
        assert "memory.mapping" in spec, (
            "debug-client spec 必须包含 'memory.mapping' — "
            "PPSSPP 注册 memory.mapping 用于地址空间列表。"
            "如果此断言失败，E-3 回归（spec 错误说 memory.info.list）。"
        )

    def test_debug_client_spec_no_memory_info_list_as_event_name(self):
        """E-3 回归：不应将 "memory.info.list" 作为活动事件名。

        spec 中 memory.info.list 可出现在历史说明或 "not X" 上下文中
        （如 `not "memory.info.list"`），但不应出现在 transport.call()
        代码示例中作为实际调用的事件名。当前 spec 使用
        `self._transport.call("memory.mapping")` — 正确。
        """
        spec = _read_spec("debug-client")
        # 精确匹配: spec 不应展示 transport.call("memory.info.list") 代码示例。
        # 这捕获 "将 memory.info.list 作为活动事件名" 的回归，
        # 同时允许历史说明中引用 memory.info.list（包括 "not X" 上下文）。
        assert 'transport.call("memory.info.list"' not in spec, (
            "debug-client spec 不应在 transport.call() 代码示例中使用 "
            "'memory.info.list' 作为事件名 — PPSSPP 使用 memory.mapping。"
            "E-3 回归。历史说明中可引用 memory.info.list（包括 "
            "'not \"memory.info.list\"' 上下文），但代码示例应使用 "
            "memory.mapping。"
        )
        # 同时检查 WS event 声明形式不应使用 memory.info.list 作为活动事件
        assert 'WS event "memory.info.list"' not in spec, (
            "debug-client spec 不应将 'memory.info.list' 声明为 "
            "WS event（活动事件名）— PPSSPP 使用 memory.mapping。"
            "E-3 回归。"
        )
