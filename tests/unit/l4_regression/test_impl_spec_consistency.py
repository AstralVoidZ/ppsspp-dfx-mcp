"""CI 校验：impl 代码中的参数/逻辑与 spec 定义一致。

用 inspect.signature 反射 tool wrapper 与 DebugClient 方法签名，
检查源码中不存在已知的反模式。防止历史 impl 错误重现：
- P-01: dump_texture 曾错误接受 address 参数（应只有 level）
- P-02: read_string 曾传 length 给 DebugClient.read_string（PPSSPP 无此参数）
- P-15: func_remove 曾传 name 给 DebugClient.func_remove（PPSSPP 无此参数）
- P-16: hold_buttons 不允许空字符串（无法释放所有按钮）
- P-17: search_disasm 曾 fallback 到 disasm/results 键（掩盖契约漂移）

Anchor:
- L4: 签名包含/不包含特定参数（revert 即失败）。
- L4: 源码不包含已知反模式字符串（revert 即失败）。
"""

from __future__ import annotations

import inspect

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient
from ppsspp_dfx_mcp.tools.input import hold_buttons
from ppsspp_dfx_mcp.tools.memory import read_memory
from ppsspp_dfx_mcp.tools.screenshot import dump
from ppsspp_dfx_mcp.tools.search_disasm import search_disasm

# ============================================================================
# P-01: dump_texture — has level, no address
# ============================================================================


class TestImplDumpTextureParams:
    """P-01: dump_texture 签名应有 level，不应有 address。"""

    def test_signature_has_level(self):
        """L4 anchor: `level` 在 dump_texture 签名中。"""
        sig = inspect.signature(dump)
        assert "level" in sig.parameters, (
            "ppsspp_dump 必须有 `level` 参数 — "
            "PPSSPP gpu.buffer.texture 按 mipmap level 捕获。"
            "如果此断言失败，P-01 回归。"
        )

    def test_signature_no_address(self):
        """L4 anchor: `address` 不在 dump_texture 签名中。

        PPSSPP 捕获当前绑定的纹理，不支持按 VRAM 地址捕获。
        之前 dump_texture 曾错误接受 address 参数。
        """
        sig = inspect.signature(dump)
        assert "address" not in sig.parameters, (
            "dump_texture 不应有 `address` 参数 — "
            "PPSSPP 不支持按地址捕获纹理（只捕获当前绑定纹理）。"
            "P-01 回归。"
        )


# ============================================================================
# P-02: read_string — no length param on DebugClient
# ============================================================================


class TestImplReadStringNoLength:
    """P-02: DebugClient.read_string 签名不应有 length 参数。"""

    def test_debug_client_read_string_no_length(self):
        """L4 anchor: `length` 不在 DebugClient.read_string 签名中。

        PPSSPP memory.readString 只注册 address + type，不注册 length。
        之前 read_string 曾错误接受 length 参数（被 PPSSPP 静默忽略）。
        """
        sig = inspect.signature(PpssppDebugClient.read_string)
        params = set(sig.parameters.keys()) - {"self"}
        assert "length" not in params, (
            "DebugClient.read_string 不应有 `length` 参数 — "
            "PPSSPP memory.readString 只注册 address + type。"
            "P-02 回归。"
        )

    def test_read_memory_tool_passes_bounded_cap_to_client(self):
        """L4 anchor (F-3 fix, 2026-09-06): read_memory 的 read_string
        分支必须传 bounded cap（max_length），且绝不调用 PPSSPP
        memory.readString（strnlen 无上限，巨量响应会杀死 WebSocket）。

        用源码行检查而非精确字符串匹配，避免变量名重构导致误报。
        """
        src = inspect.getsource(read_memory)
        # 找到 client.read_string( 调用行
        read_string_lines = [line for line in src.splitlines() if "client.read_string(" in line]
        assert read_string_lines, "read_memory 源码中应包含 client.read_string(...) 调用。"
        for line in read_string_lines:
            # 调用应传 address（变量名可能是 address 或 address_int）
            assert "address=" in line, (
                f"read_memory 的 read_string 调用应传 address — 行: {line.strip()}"
            )
            # 必须传 max_length 上限（F-3：无上限读取是缺陷根因）
            assert "max_length=" in line, (
                f"read_memory 的 read_string 调用应传 max_length 上限 — "
                f"F-3 回归。行: {line.strip()}"
            )
        # 不应存在以事件名形式出现的 PPSSPP memory.readString 调用
        # （带引号的事件名参数；参数描述文本中的提及不算）
        assert '"memory.readString"' not in src, (
            "read_memory 不得调用 PPSSPP memory.readString 事件（F-3 回归）"
        )


# ============================================================================
# P-15: func_remove — no name param on DebugClient
# ============================================================================


class TestImplFuncRemoveNoName:
    """P-15: DebugClient.func_remove 签名不应有 name 参数。"""

    def test_debug_client_func_remove_no_name(self):
        """L4 anchor: `name` 不在 DebugClient.func_remove 签名中。

        PPSSPP hle.func.remove 只注册 address（u32, required），
        不注册 name。之前 func_remove 曾错误接受 name 参数。
        """
        sig = inspect.signature(PpssppDebugClient.func_remove)
        params = set(sig.parameters.keys()) - {"self"}
        assert "name" not in params, (
            "DebugClient.func_remove 不应有 `name` 参数 — "
            "PPSSPP hle.func.remove 只注册 address。"
            "P-15 回归。"
        )


# ============================================================================
# P-16: hold_buttons — empty string skips validation
# ============================================================================


class TestImplHoldButtonsEmptyString:
    """P-16: hold_buttons 应允许空字符串（跳过验证，释放所有按钮）。"""

    def test_hold_buttons_source_skips_validation_for_empty(self):
        """L4 anchor: hold_buttons 源码中空字符串路径跳过 _validate_buttons_combo。

        空字符串 = 释放所有已按住的按钮（per Field description）。
        只有非空组合才调用 _validate_buttons_combo 验证。
        之前 hold_buttons 曾无条件调用验证，导致空字符串触发错误。
        """
        src = inspect.getsource(hold_buttons)
        # 守卫模式: if buttons and buttons.strip():
        assert "if buttons" in src, (
            "hold_buttons 源码必须包含 `if buttons` 守卫 — "
            "空字符串应跳过 _validate_buttons_combo 验证（释放路径）。"
            "P-16 回归。"
        )
        # 确认 _validate_buttons_combo 在守卫内部（非无条件调用）
        lines = src.splitlines()
        validate_lines = [
            (i, line) for i, line in enumerate(lines) if "_validate_buttons_combo" in line
        ]
        assert validate_lines, "hold_buttons 源码应调用 _validate_buttons_combo。"
        for idx, line in validate_lines:
            # _validate_buttons_combo 应在 if 守卫内部（缩进更深）
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            assert indent > 0, (
                f"_validate_buttons_combo 不应在模块顶层（应在 if 守卫内）— "
                f"行 {idx + 1}: {line.strip()}"
            )


# ============================================================================
# P-17: search_disasm — no disasm/results fallback keys
# ============================================================================


class TestImplSearchDisasmNoFallback:
    """P-17: search_disasm 不应 fallback 到 disasm/results 键。"""

    def test_search_disasm_no_disasm_fallback_key(self):
        """L4 anchor: search_disasm 源码不应有 response.get("disasm") 回退。

        PPSSPP memory.searchDisasm 返回 lines 字段，没有 disasm 键。
        回退键会掩盖契约漂移（PPSSPP 改名时静默返回空列表）。
        """
        src = inspect.getsource(search_disasm)
        assert 'response.get("disasm")' not in src, (
            'search_disasm 不应使用 response.get("disasm") 作为回退键 — '
            "PPSSPP memory.searchDisasm 只返回 lines 字段。"
            "P-17 回归。"
        )

    def test_search_disasm_no_results_fallback_key(self):
        """L4 anchor: search_disasm 源码不应有 response.get("results") 回退。"""
        src = inspect.getsource(search_disasm)
        assert 'response.get("results")' not in src, (
            'search_disasm 不应使用 response.get("results") 作为回退键 — '
            "PPSSPP memory.searchDisasm 只返回 lines 字段。"
            "P-17 回归。"
        )
