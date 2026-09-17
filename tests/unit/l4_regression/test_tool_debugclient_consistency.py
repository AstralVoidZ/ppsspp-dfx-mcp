"""CI 校验：tool wrapper 与底层 DebugClient/CaptureService 函数签名一致。

直接对照 tools/*.py 中的
tool wrapper 与 service/debug_client.py 中的 DebugClient 方法签名，
确保参数在传递链中不丢失、不引入幽灵参数。

防止历史不一致重现：
- P-01: dump_texture 参数应与 dump_texture 工具一致（都有 level，无 address）
- P-02: read_string — DebugClient 无 length 参数（tool wrapper 无法传递）
- P-15: func_remove — DebugClient 无 name 参数（tool wrapper 无法传递）

Anchor:
- L4: 签名层面的参数集合对照（revert 即失败）。
"""

from __future__ import annotations

import inspect

from ppsspp_dfx_mcp.service.capture import CaptureService
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient
from ppsspp_dfx_mcp.tools.screenshot import dump

# ============================================================================
# dump_texture ↔ dump_texture: 参数一致
# ============================================================================


class TestToolDebugClientDumpTextureConsistency:
    """dump_texture 与 dump_texture 工具签名一致。"""

    def test_both_have_level_param(self):
        """L4 anchor: 两者都有 `level` 参数。"""
        async_sig = inspect.signature(CaptureService.dump_texture)
        tool_sig = inspect.signature(dump)
        assert "level" in async_sig.parameters, "CaptureService.dump_texture 必须有 `level` 参数。"
        assert "level" in tool_sig.parameters, "ppsspp_dump 工具必须有 `level` 参数。"

    def test_neither_has_address_param(self):
        """L4 anchor: 两者都没有 `address` 参数（P-01 回归）。

        PPSSPP 捕获当前绑定的纹理，不支持按 VRAM 地址捕获。
        dump_texture 和 dump_texture 都不应接受 address。
        """
        async_sig = inspect.signature(CaptureService.dump_texture)
        tool_sig = inspect.signature(dump)
        assert "address" not in async_sig.parameters, (
            "CaptureService.dump_texture 不应有 `address` 参数 — PPSSPP 不支持按地址捕获纹理。P-01 回归。"
        )
        assert "address" not in tool_sig.parameters, (
            "ppsspp_dump 工具不应有 `address` 参数 — PPSSPP 不支持按地址捕获纹理。P-01 回归。"
        )

    def test_debugclient_param_sets_match(self):
        """L4 anchor: 纹理捕获参数链一致（level 存在、address 不存在）。

        v0.1.6 起 tool 侧合并为 ppsspp_dump(kind, session_id, level)，
        服务侧仍为 CaptureService.dump_texture(level)；参数链一致性改按
        关键参数存在性 + 幽灵参数缺失双重断言。
        """
        async_params = set(inspect.signature(CaptureService.dump_texture).parameters.keys())
        tool_params = set(inspect.signature(dump).parameters.keys())
        assert "level" in async_params and "level" in tool_params, (
            f"参数链必须都有 level：service={async_params}, tool={tool_params}"
        )
        assert "address" not in async_params and "address" not in tool_params, (
            f"参数链不得引入 address：service={async_params}, tool={tool_params}"
        )


# ============================================================================
# read_string: DebugClient 无 length（tool wrapper 无法传递）
# ============================================================================


class TestToolDebugClientReadStringConsistency:
    """read_string — DebugClient 签名无 length，tool wrapper 无法传递。"""

    def test_debug_client_read_string_no_length(self):
        """L4 anchor: DebugClient.read_string 签名无 `length` 参数。

        即使 tools/memory.py:read_memory 保留了 length 参数
        （向后兼容），DebugClient.read_string 不接受 length，
        因此 length 永远不会被传递给 PPSSPP。
        """
        sig = inspect.signature(PpssppDebugClient.read_string)
        params = set(sig.parameters.keys()) - {"self"}
        assert "length" not in params, (
            "DebugClient.read_string 不应有 `length` 参数 — "
            "PPSSPP memory.readString 只注册 address + type。"
            "P-02 回归。"
        )

    def test_debug_client_read_string_has_address_and_encoding(self):
        """L4 anchor: DebugClient.read_string 有 address + encoding 参数。

        Python 签名用 `encoding`（避免遮蔽内置 type）；转发到 PPSSPP
        的 WS 事件参数名仍然是 `type`（见 L1 test_read_string_*）。
        """
        sig = inspect.signature(PpssppDebugClient.read_string)
        params = set(sig.parameters.keys()) - {"self"}
        assert "address" in params, "DebugClient.read_string 必须有 `address` 参数。"
        assert "encoding" in params, (
            "DebugClient.read_string 必须有 `encoding` 参数 "
            "（默认 'utf-8'，转发到 WS 事件 `type`）。"
        )


# ============================================================================
# func_remove: DebugClient 无 name（tool wrapper 无法传递）
# ============================================================================


class TestToolDebugClientFuncRemoveConsistency:
    """func_remove — DebugClient 签名无 name，tool wrapper 无法传递。"""

    def test_debug_client_func_remove_no_name(self):
        """L4 anchor: DebugClient.func_remove 签名无 `name` 参数。

        即使 tools/query.py:query 保留了 name 参数（用于 func_add
        等其他 action），DebugClient.func_remove 不接受 name，
        因此 name 永远不会被传递给 PPSSPP 的 hle.func.remove。
        """
        sig = inspect.signature(PpssppDebugClient.func_remove)
        params = set(sig.parameters.keys()) - {"self"}
        assert "name" not in params, (
            "DebugClient.func_remove 不应有 `name` 参数 — "
            "PPSSPP hle.func.remove 只注册 address。"
            "P-15 回归。"
        )

    def test_debug_client_func_remove_has_address(self):
        """L4 anchor: DebugClient.func_remove 有 address 参数（required）。"""
        sig = inspect.signature(PpssppDebugClient.func_remove)
        params = sig.parameters
        assert "address" in params, "DebugClient.func_remove 必须有 `address` 参数。"
        assert params["address"].default is inspect.Parameter.empty, (
            "DebugClient.func_remove `address` 应为必填参数（无默认值）。"
        )
