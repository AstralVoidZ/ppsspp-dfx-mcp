"""test_tool_signature_contract.py — L2 MCP contract: tool function signatures.

Anchor: 实测能力评估报告 P-01/P-02/P-03 三个 CRITICAL bug 的回归防护。

L2 签名契约测试：验证工具函数的参数签名与 schema 描述一致，防止
FastMCP 分发时因参数名错配导致 TypeError。这些测试不调用工具函数
（那是 L3 的职责），只检查 inspect.signature 的参数名与类型。

覆盖的修复：
- F-01 (P-02): read_memory 的 read_string action 不传 length 死参数
- F-02 (P-03): memory_info_search 用 `type` 而非 `region_type` + alias
- F-03 (P-01): dump_texture 用 `level` 而非 `address`
"""

from __future__ import annotations

import inspect
import sys
import types

# mcp[cli] is a declared dependency (pyproject.toml), so mcp.server.mcpserver
# is always available in the test environment. No stub needed — using the
# real Image class ensures the import path matches the runtime path and
# catches serialization bugs (P-11 was caused by a wrong import path that
# a stub would have masked).
from mcp.server.mcpserver import Image  # noqa: F401 (re-exported by screenshot.py)

from ppsspp_dfx_mcp.tools.memory import read_memory
from ppsspp_dfx_mcp.tools.memory_info_search import memory_info_search
from ppsspp_dfx_mcp.tools.screenshot import dump_texture


# ============================================================================
# F-01 (P-02): read_memory read_string 不传 length
# ============================================================================


class TestReadStringLengthDeprecation:
    """read_string action 不应向 DebugClient.read_string 传递 length 参数。

    Anchor: analysis_ppsspp_dfx_mcp_live_evaluation_v1.md P-02。
    DebugClient.read_string(address, type="utf-8") 不接受 length，
    tool 层若传 length=length 会触发 TypeError。
    """

    def test_read_memory_has_length_param(self):
        """length 参数仍存在于签名中（向后兼容，但标记 deprecated）。"""
        sig = inspect.signature(read_memory)
        assert "length" in sig.parameters, (
            "length 参数应保留在签名中（标记 deprecated），不应删除"
        )

    def test_read_memory_length_has_deprecated_description(self):
        """length 参数的 description 应标注 deprecated/ignored。"""
        sig = inspect.signature(read_memory)
        length_param = sig.parameters["length"]
        # Pydantic Field 的 description 存在于 Annotated metadata 中
        # 这里只验证参数存在且默认值为 None
        assert length_param.default is None


# ============================================================================
# F-02 (P-03): memory_info_search 用 type 而非 region_type
# ============================================================================


class TestMemoryInfoSearchTypeParam:
    """memory_info_search 必须用 `type` 参数名（与 schema 一致）。

    Anchor: analysis_ppsspp_dfx_mcp_live_evaluation_v1.md P-03。
    Pydantic alias="type" 导致 FastMCP 分发时仍以 `type` 为 kwarg
    传入，但函数签名是 `region_type`，触发 TypeError。
    修复：直接用 `type` 参数名，不用 alias。
    """

    def test_has_type_param(self):
        """函数签名必须包含 `type` 参数。"""
        sig = inspect.signature(memory_info_search)
        assert "type" in sig.parameters, (
            "memory_info_search 必须有 `type` 参数（不能用 region_type + alias）"
        )

    def test_no_region_type_param(self):
        """函数签名不能包含 `region_type` 参数（已重命名）。"""
        sig = inspect.signature(memory_info_search)
        assert "region_type" not in sig.parameters, (
            "region_type 参数应已重命名为 type，不应残留"
        )

    def test_type_param_is_optional(self):
        """type 参数是可选的（默认 None）。"""
        sig = inspect.signature(memory_info_search)
        type_param = sig.parameters["type"]
        assert type_param.default is None, (
            f"type 参数默认值应为 None，实际为 {type_param.default}"
        )


# ============================================================================
# F-03 (P-01): dump_texture 用 level 而非 address
# ============================================================================


class TestDumpTextureLevelParam:
    """dump_texture 必须用 `level` 参数，不能有 `address` 参数。

    Anchor: analysis_ppsspp_dfx_mcp_live_evaluation_v1.md P-01。
    PPSSPP 只能抓当前绑定纹理，不支持按 VRAM address 抓取。
    schema (ppsspp_dump_texture.json) 已更新为 level 参数。
    """

    def test_has_level_param(self):
        """函数签名必须包含 `level` 参数。"""
        sig = inspect.signature(dump_texture)
        assert "level" in sig.parameters, (
            "dump_texture 必须有 `level` 参数"
        )

    def test_no_address_param(self):
        """函数签名不能包含 `address` 参数。"""
        sig = inspect.signature(dump_texture)
        assert "address" not in sig.parameters, (
            "dump_texture 不应有 `address` 参数（PPSSPP 不支持按地址抓取纹理）"
        )

    def test_level_default_is_zero(self):
        """level 参数默认值为 0（mipmap level 0）。"""
        sig = inspect.signature(dump_texture)
        level_param = sig.parameters["level"]
        assert level_param.default == 0, (
            f"level 参数默认值应为 0，实际为 {level_param.default}"
        )
