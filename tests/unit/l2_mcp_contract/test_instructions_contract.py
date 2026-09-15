"""test_instructions_contract.py — L2 契约：MCP `instructions` 的结构与事实。

`instructions` 在 `initialize` 时返回一次，被客户端注入模型上下文并**全程驻留**。
它因此是唯一能承载**跨工具知识**的通道（调用顺序、session_id 规则、地址陷阱、
上下文预算、错误码首步），也是唯一一处"写错了会持续污染全局上下文"的文本。

本文件把三件事钉住：

1. **结构**：分节齐全且有序——它是可扫读的保证，节丢了不会有人察觉。
2. **体积上限**：instructions 挤占的是模型的系统提示。它能膨胀到失去作用，
   而膨胀是渐进的、无人反对的。设一个上限，让扩张必须是有意识的行为。
3. **载重事实与代码一致**：尤其地址陷阱那条——它描述的是 `parse_address` 的
   **当前**行为（裸数字串按十进制解析）。若将来修好了解析器，这条指令就变成
   假话，而本测试会同时变红，逼着两者一起改。
   **这就是"文档与代码耦合"的可执行形式**——否则只能靠记忆维护。
"""
from __future__ import annotations

import re

from ppsspp_dfx_mcp.instructions import INSTRUCTIONS

#: 体积上限（字符）。当前约 2.3K；旧版（6 段密排散文）约 1.1K。
#: 上限存在的意义不是"越小越好"，而是让每次扩张都必须是一次有意识的决定。
_MAX_CHARS = 3200

#: 载重小节，按应出现的顺序。
_SECTIONS = (
    "## Bring-up",
    "## session_id",
    "## Addresses",
    "## Context budget",
    "## Breakpoints",
    "## Reading errors",
)

#: 可省略 session_id 的工具（与 `resolve_session_id` 的实际调用点一致）。
_AUTO_SESSION_ID_TOOLS = (
    "ppsspp_read_memory",
    "ppsspp_disassemble",
    "ppsspp_get_pc",
    "ppsspp_step",
    "ppsspp_screenshot",
)


def _served_instructions() -> str:
    """握手实际下发的 instructions（而非直接读常量）。"""
    from ppsspp_dfx_mcp.server import mcp

    return mcp._lowlevel_server.create_initialization_options().instructions or ""


class TestStructure:
    def test_sections_present_in_order(self):
        positions = []
        for section in _SECTIONS:
            idx = INSTRUCTIONS.find(section)
            assert idx != -1, f"instructions 缺少小节 {section!r}"
            positions.append(idx)
        assert positions == sorted(positions), (
            f"小节顺序被打乱：{list(zip(_SECTIONS, positions))}"
        )

    def test_size_ceiling(self):
        assert len(INSTRUCTIONS) <= _MAX_CHARS, (
            f"instructions 已增长到 {len(INSTRUCTIONS)} 字符（上限 {_MAX_CHARS}）。"
            f"它挤占的是模型系统提示：新增内容前先确认它**不是**某个工具 "
            f"description 已有的信息——跨工具知识才属于这里。"
        )

    def test_not_truncated(self):
        """下限：防止误删到只剩标题。"""
        assert len(INSTRUCTIONS) > 800

    def test_handshake_serves_the_constant(self):
        """握手下发的必须就是本文件断言的那份文本。"""
        assert _served_instructions() == INSTRUCTIONS


class TestLoadBearingFacts:
    """每条断言对应一条"写错了会误导入门者"的事实。"""

    def test_auto_resolving_tools_are_named(self):
        for tool in _AUTO_SESSION_ID_TOOLS:
            assert tool in INSTRUCTIONS, (
                f"instructions 应点名可省略 session_id 的工具 {tool}"
            )

    def test_auto_resolving_tool_set_matches_the_code(self):
        """点名的 5 个工具必须真的是自动解析的那 5 个。

        `resolve_session_id` 是唯一的自动解析入口；若某工具新增/移除了它，
        本断言变红——instructions 里的名单随之必须更新。
        """
        import ast
        from pathlib import Path

        tools_dir = Path(__file__).resolve().parents[3] / "src" / "ppsspp_dfx_mcp" / "tools"
        found: set[str] = set()
        for f in tools_dir.glob("*.py"):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                if not any(
                    getattr(d, "attr", "") == "tool"
                    or (isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "tool")
                    for d in node.decorator_list
                ):
                    continue
                if "resolve_session_id" not in ast.dump(node):
                    continue
                name = node.name
                for d in node.decorator_list:
                    if isinstance(d, ast.Call):
                        for kw in d.keywords:
                            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                                name = kw.value.value
                found.add(name)
        assert found == set(_AUTO_SESSION_ID_TOOLS), (
            f"自动解析 session_id 的工具集变了：\n"
            f"  代码里有、instructions 没列: {sorted(found - set(_AUTO_SESSION_ID_TOOLS))}\n"
            f"  instructions 列了、代码里没有: {sorted(set(_AUTO_SESSION_ID_TOOLS) - found)}"
        )

    def test_address_trap_claim_matches_the_parser(self):
        """地址陷阱那条必须是**当前**解析器的真实行为。

        这条断言把文档钉在代码上：`parse_address("08804000")` 现在按十进制解析
        成 8804000。若解析器将来改为拒绝裸数字串，本断言变红，instructions 中
        「A bare digit string … is NOT rejected」随之必须删除或改写。
        """
        from ppsspp_dfx_mcp.address import format_address, parse_address

        parsed = parse_address("08804000")
        assert parsed == 8804000, (
            f"parse_address('08804000') 现在返回 {parsed}，与 instructions 的"
            f"「按十进制解析成 8804000」不符——请同步更新 instructions.py"
        )
        # instructions 引用的对照值也要对得上。**用 format_address 计算，不要
        # 手写**：本断言首次运行时抓到的就是作者手写错的十六进制值。
        assert format_address(8804000) in INSTRUCTIONS

    def test_error_codes_in_triage_are_real(self):
        """错误码分诊表里的每个码都必须是真实存在的 `ToolError.code`。

        拼错一个码，Agent 就再也匹配不上它要处理的错误——而这段文本正是
        它唯一的首步指引。

        码的声明有**两条**来源，都要收：类属性（`class SessionNotFound:
        code = "..."`）与内联 kwarg（`ToolError(msg, code="CAPTURE_EMPTY")`）。
        只收类属性会漏掉后者——本断言首次运行即因此误报了 `CAPTURE_EMPTY`
        与 `PROTECTED_ADDRESS`。
        """
        import re as _re
        from pathlib import Path as _Path

        from ppsspp_dfx_mcp import errors as E

        real: set[str] = set()
        stack = [E.ToolError]
        while stack:
            cls = stack.pop()
            code = getattr(cls, "code", None)
            if isinstance(code, str):
                real.add(code)
            stack.extend(cls.__subclasses__())

        src_root = _Path(__file__).resolve().parents[3] / "src" / "ppsspp_dfx_mcp"
        for f in src_root.rglob("*.py"):
            real.update(
                _re.findall(r'code=["\']([A-Z][A-Z_]+)["\']', f.read_text(encoding="utf-8"))
            )

        triage = INSTRUCTIONS.split("## Reading errors", 1)[1]
        listed = set(re.findall(r"^\s{2}([A-Z][A-Z_]{4,})\s+→", triage, re.MULTILINE))
        assert listed, "分诊表解析失败——格式可能变了"
        unknown = sorted(listed - real)
        assert not unknown, (
            f"instructions 分诊表引用了不存在的错误码：{unknown}\n"
            f"（真实码：{sorted(real)}）"
        )

    def test_breakpoint_prerequisite_stated(self):
        """CPUCore=2 是**静默**前提（JIT 下断点永不触发、不报错）。"""
        assert "CPUCore=2" in INSTRUCTIONS

    def test_points_at_the_skill_for_the_full_matrix(self):
        assert "ppsspp-dfx skill" in INSTRUCTIONS


class TestNoDuplicationOfToolDescriptions:
    """instructions 不得复述工具自身 description 已写明的机制。

    这是本文件唯一一条**方向性**断言：反向（"某些词不得出现"）会误伤，故只
    检查几个已知的、曾被错误重复过的具体机制——它们都是工具 description 里
    的原文职责，出现在 instructions 就意味着出现了第二份会漂移的副本。
    """

    def test_does_not_restate_tool_level_mechanics(self):
        banned = {
            "resilient=true": "start(resilient=true) 的语义在 ppsspp_session 描述里",
            "already_paused": "已暂停 CPU 的返回语义在 ppsspp_wait_breakpoint 描述里",
            "65536": "单次读上限在 read_memory 的参数与描述里",
            "top_n": "func_list 截断参数在 ppsspp_query 描述里",
        }
        hits = [why for token, why in banned.items() if token in INSTRUCTIONS]
        assert not hits, "instructions 复述了工具描述已有的机制：\n  " + "\n  ".join(hits)
