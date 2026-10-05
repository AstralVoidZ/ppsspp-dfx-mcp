"""specs/010 US4 / T048 — 错误翻译的「双轨」必须可判等（契约 guards.md C4C-1…C4C-4）。

背景（实测）：`tools/_common.py::translate_tool_errors` 是一个**装饰器**——`ToolError`
原样重抛，其余异常经 `to_tool_error(e)` 翻译。同时**部分函数体内还有内联
`raise to_tool_error(...)`**。于是同一工具存在两条翻译轨道；装饰器已覆盖的函数里再写
内联翻译，就是**真冗余**（减法对象）；而未被覆盖的（同步工具、资源端点、装饰器自身）
**必须保留**——误删会静默丢失翻译。

判据：

  E-1  翻译的**锚点性质**：已是 `ToolError` 的异常 MUST 原样返回（同一对象）。
        这是两条轨道不会分叉的前提——否则"已翻译"会被二次包装、改码。
  E-2  `to_tool_error` MUST **全函数且确定**：任意异常都产出 `ToolError`，
        且同一异常两次调用得到同一类型与同一 code。
  E-3  被装饰器覆盖的函数 MUST NOT 含内联翻译 —— 当前红，即减法清单（机械判定，
        不靠人工判断"看着像冗余"）。
  E-4  未被覆盖的内联翻译 MUST 登记且**附理由** —— 新增一处就必须显式说明为何保留。

**范围声明（与任务清单的差异，如实记录）**：任务清单写的是"对全部 37 个工具做异常注入"。
多数工具在没有活动调试器会话时会在参数校验/连接阶段就失败，**无法把异常注入到翻译点**，
强行执行只会得到与翻译无关的连接错误。故本判据落在**机制层等价**（E-1/E-2）
+ **AST 锁定的存量清单**（E-3/E-4）：前者证明两条轨道语义一致，后者保证减法范围
可复核、不漏项。真正的逐工具注入需设备会话，属 US3 的 W-1/W-5 范畴。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PKG = Path(__file__).resolve().parents[3]
_SRC = _PKG / "src" / "ppsspp_dfx_mcp"

#: 翻译装饰器的名字（判定"是否已覆盖"的唯一依据）。
DECORATOR = "translate_tool_errors"

#: E-4 的保留登记：`文件::函数` → 保留理由。新增未覆盖的内联翻译 MUST 显式登记。
KEEP_REGISTRY: dict[str, str] = {
    "resources.py::game_state": "资源端点不走工具装饰器（装饰器只包 async 工具函数）",
    "resources.py::registers": "同上：资源端点无装饰器覆盖",
    "tools/_common.py::translate_tool_errors": "装饰器自身的实现，不是冗余",
    "tools/script.py::list_scripts": "同步工具：按设计保留显式 try/except（装饰器只包 async）",
    "tools/script.py::reload_scripts": "同步工具：同上（与 list_scripts 一对，刻意保留显式翻译）",
    "tools/smoke.py::run_smoke_checks": "冒烟入口未被装饰器覆盖",
}


def _decorated_functions(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                name = target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
                if name == DECORATOR:
                    out.add(node.name)
    return out


def _inline_translation_sites() -> list[tuple[str, str, int]]:
    """[(文件相对路径, 所属函数, 行号)] —— 所有内联 `raise to_tool_error(...)`。"""
    sites: list[tuple[str, str, int]] = []
    for py in sorted(_SRC.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        rel = py.relative_to(_SRC).as_posix()
        tree = ast.parse(py.read_text(encoding="utf-8"))
        covered = _decorated_functions(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
                continue
            fn = node.exc.func
            name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if name != "to_tool_error":
                continue
            owner = "<module>"
            for cand in ast.walk(tree):
                if isinstance(cand, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                    node is d for d in ast.walk(cand)
                ):
                    owner = cand.name
                    break
            sites.append((rel, owner, node.lineno))
            if owner in covered:
                sites[-1] = (rel, owner, -node.lineno)  # 负号标记 = 冗余候选
    return sites


@pytest.fixture(scope="module")
def sites() -> list[tuple[str, str, int]]:
    return _inline_translation_sites()


# ── E-1 / E-2 机制层等价 ──────────────────────────────────────────────────
def test_already_translated_errors_pass_through_unchanged() -> None:
    """E-1：两条轨道的交点是"已翻译的 `ToolError`"。若这里发生二次包装或改码，
    装饰器通路与内联通路就会对同一错误产出不同分类——双轨立刻分叉。"""
    from ppsspp_dfx_mcp.errors import ToolError, to_tool_error

    class _Sentinel(ToolError):
        pass

    original = _Sentinel("probe")
    assert to_tool_error(original) is original, "已是 ToolError 的异常必须原样返回"


def test_translation_is_total_and_deterministic() -> None:
    """E-2：任意异常都产出 ToolError，且同一异常两次翻译结果一致（分类码不漂移）。"""
    from ppsspp_dfx_mcp.errors import ToolError, to_tool_error

    probes: list[Exception] = [
        ValueError("bad"),
        TimeoutError("t"),
        KeyError("k"),
        RuntimeError("r"),
        OSError("io"),
        ZeroDivisionError("z"),
    ]
    for exc in probes:
        first = to_tool_error(exc)
        second = to_tool_error(exc)
        assert isinstance(first, ToolError), f"{type(exc).__name__} 未被翻译为 ToolError"
        assert type(first) is type(second), f"{type(exc).__name__} 两次翻译类型不一致"
        assert getattr(first, "code", None) == getattr(second, "code", None), (
            f"{type(exc).__name__} 两次翻译 code 不一致"
        )


# ── E-3 冗余候选（减法清单）────────────────────────────────────────────────
def test_decorated_functions_carry_no_inline_translation(sites) -> None:
    """E-3：装饰器已覆盖的函数里再内联翻译 = 真冗余（装饰器会先翻译并重抛）。

    这条判据同时充当 specs/010 T054 的**减法清单来源**——由 AST 机械判定，
    不依赖"看着像冗余"的人工判断。
    """
    redundant = sorted({(rel, owner) for rel, owner, line in sites if line < 0})
    assert not redundant, (
        f"被 `{DECORATOR}` 覆盖的函数里仍有内联翻译（E-3，共 {len(redundant)} 个函数）："
        f"{redundant}\n"
        "        这些是纯冗余：装饰器会先翻译并原样重抛 ToolError。\n"
        "        删除前请确认该函数体内没有**装饰器覆盖不到**的分支"
        "（例如仅在特定参数下才走到的早退路径）。"
    )


# ── E-4 保留登记 ──────────────────────────────────────────────────────────
def test_uncovered_inline_translations_are_registered_with_a_reason(sites) -> None:
    """E-4：未被装饰器覆盖的内联翻译 MUST 显式登记并附理由。

    没有这条，"看起来冗余"就会被当成人人可删——而同步工具/资源端点的内联翻译
    删掉会**静默丢失翻译**（异常直接以原始类型冒到客户端）。
    """
    uncovered = sorted({f"{rel}::{owner}" for rel, owner, line in sites if line > 0})
    unregistered = [k for k in uncovered if k not in KEEP_REGISTRY]
    assert not unregistered, (
        f"以下未覆盖的内联翻译未登记保留理由：{unregistered}\n"
        "        新增一处 MUST 显式登记并说明为何不能删（判据：C4C-3）。"
    )
    stale = sorted(set(KEEP_REGISTRY) - set(uncovered))
    assert not stale, f"登记里有已不存在的条目（请删除）：{stale}"
    thin = {k: v for k, v in KEEP_REGISTRY.items() if len(v.strip()) < 8}
    assert not thin, f"保留理由过于简略，等于没写：{thin}"


def test_inventory_is_not_vacuous(sites) -> None:
    """防空转判据的两阶段语义：

    红灯期（债务未偿）：清单 MUST 非空 —— 否则无法区分『AST 规则失配』与『债务清零』。
    绿灯后（债务已偿）：内联清单为空是**正确结果**，此时改验扫描器仍有鉴别力 ——
    它必须仍能找到 KEEP_REGISTRY 里那些**合法保留**的未覆盖点（否则规则失配，绿是假的）。
    """
    if len(sites) >= 10:
        return  # 红灯期形态：债务未偿，清单非空
    uncovered = {f"{rel}::{owner}" for rel, owner, line in sites if line > 0}
    registered = set(KEEP_REGISTRY)  # 同文件单一来源
    assert uncovered == registered, (
        f"扫描器失配：未覆盖点 {sorted(uncovered)} 与保留登记 {sorted(registered)} 不一致"
    )
