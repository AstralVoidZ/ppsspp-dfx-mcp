"""specs/010 US4 / T047 — 共享状态的「重置缝」必须收敛（契约 guards.md C4B-1…C4B-4）。

背景（实测）：生产包里有 **9 个模块级可变状态容器**，其中

* 2 个**没有任何回收 API**：`server._exposed_registry`、`tools/script._module_cache`
* 2 个的"回收 API"是**生产侧的测试钩子**：`tools/diff._reset_registry_for_tests`、
  `tools/scan._reset_value_sessions_for_tests`（生产代码为测试开后门，且命名不统一）
* 若干测试**直写**生产私有状态（先污染、后断言）→ 判据与执行顺序耦合

判据：

  S-1  容器清单 MUST 与显式登记一致 —— 新增状态 MUST 被显式登记（不许悄悄多一个）
  S-2  每个容器 MUST 有回收 API
  S-3  回收 API MUST NOT 是生产侧的 `*_for_tests` 钩子（测试钩子不得住在生产包里）
  S-4  测试 MUST NOT 直写生产私有共享状态（读断言允许）
  S-5  登记的回收 API 确实把容器清空（功能判据，抽样验证——否则"有 API"也只是空话）

注意：本文件是**判据**（权威）；`mcp_test_report/tools/probe_dfx010_state.py` 是同规则的
**诊断工具**，用于人工勘察。两者独立实现是有意的：诊断工具要能在包不可导入时仍能跑。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from _support import state as state_seam  # T053 S-4：集中式测试支撑缝

_PKG = Path(__file__).resolve().parents[3]
_SRC = _PKG / "src" / "ppsspp_dfx_mcp"
_TESTS = _PKG / "tests"

#: S-4 的显式豁免：集中式测试支撑缝（唯一直写点）。
#: 豁免是有意为之——对生产私有容器的写入 MUST 收敛在该目录内。
_SEAM = _TESTS / "_support"

# ── S-1 的显式登记（新增容器 MUST 同步登记，并给出回收 API 的归属）────────────
REGISTRY: dict[str, dict[str, str]] = {
    "core/cond_filter.py::_filters": {"reclaim": "drop_session", "kind": "dict"},
    "core/value_staleness.py::_ZERO_STREAKS": {"reclaim": "reset_probe_streaks", "kind": "dict"},
    # T049：容器随组合根实例一并迁入 registry.py（登记同步更新）
    "registry.py::_exposed_registry": {"reclaim": "clear_exposed_registry", "kind": "dict"},
    "service/probe_observer.py::_REGISTRY_BY_SESSION": {"reclaim": "drop_session", "kind": "dict"},
    "service/probe_observer.py::_SEEDED_BY_SESSION": {"reclaim": "drop_session", "kind": "set"},
    "session/client_helper.py::_FAKE_TRANSPORTS": {"reclaim": "drop_session", "kind": "dict"},
    "tools/diff.py::_SNAPSHOTS": {"reclaim": "reset_snapshots", "kind": "dict"},
    "tools/scan.py::_VALUE_SESSIONS": {"reclaim": "reset_value_sessions", "kind": "dict"},
    "tools/script.py::_module_cache": {"reclaim": "_clear_module_cache", "kind": "dict"},
}

#: 会把容器写坏的属性方法（下标赋值另算）。
_MUTATORS = frozenset(
    {
        "add",
        "append",
        "clear",
        "pop",
        "popitem",
        "update",
        "setdefault",
        "remove",
        "discard",
        "insert",
        "extend",
        "difference_update",
        "intersection_update",
        "symmetric_difference_update",
        "__setitem__",
        "__delitem__",
    }
)
_MAKE = {"dict", "set", "list"}
_NON_STATE = frozenset({"__all__", "__slots__", "__path__"})


def _dotted(node: ast.AST) -> str:
    """把 `a.b.c` / `a` 还原成点分名，用于与登记名比对。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def _iter_source_files(root: Path):
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _scan_mutable_state() -> dict[str, str]:
    """枚举模块级可变容器 → {'pkg-relative::name': kind}。

    判据是**是否被写入**（下标赋值 / 变更方法调用），不是命名约定：常量查找表
    （`_TRANSITIONS` 之类）从不写入，不需要回收；`__all__` 之类的导出清单同理。
    """
    found: dict[str, str] = {}
    for py in _iter_source_files(_SRC):
        rel = py.relative_to(_SRC).as_posix()
        tree = ast.parse(py.read_text(encoding="utf-8"))
        mutated = {
            n.func.value.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in _MUTATORS
            and isinstance(n.func.value, ast.Name)
        }
        mutated |= {
            n.value.id
            for n in ast.walk(tree)
            if isinstance(n, ast.Subscript)
            and isinstance(n.ctx, ast.Store)
            and isinstance(n.value, ast.Name)
        }
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign | ast.Assign):
                continue
            target = (
                node.target
                if isinstance(node, ast.AnnAssign)
                else (node.targets[0] if node.targets else None)
            )
            if not isinstance(target, ast.Name) or not target.id.startswith("_"):
                continue
            if target.id in _NON_STATE:
                continue
            val = node.value
            kind = (
                "dict"
                if isinstance(val, ast.Dict)
                else "set"
                if isinstance(val, ast.Set)
                else "list"
                if isinstance(val, ast.List)
                else (
                    val.func.id
                    if isinstance(val, ast.Call)
                    and isinstance(val.func, ast.Name)
                    and val.func.id in _MAKE
                    else None
                )
            )
            if kind and target.id in mutated:
                found[f"{rel}::{target.id}"] = kind
    return found


@pytest.fixture(scope="module")
def actual_state() -> dict[str, str]:
    return _scan_mutable_state()


# ── S-1 ───────────────────────────────────────────────────────────────────
def test_registry_matches_the_code(actual_state: dict[str, str]) -> None:
    """容器清单 MUST 与登记一致：多一个少一个都要显式面对。"""
    missing = sorted(set(actual_state) - set(REGISTRY))
    extra = sorted(set(REGISTRY) - set(actual_state))
    assert not missing, (
        f"新增了未登记的模块级可变状态：{missing}\n"
        "        新增共享状态 MUST 同步登记，并给出回收 API——否则测试只能靠顺序隔离它。"
    )
    assert not extra, f"登记里有已不存在的容器（请删除登记项）：{extra}"


def test_registry_kind_is_accurate(actual_state: dict[str, str]) -> None:
    wrong = {
        key: (REGISTRY[key]["kind"], actual_state[key])
        for key in set(REGISTRY) & set(actual_state)
        if REGISTRY[key]["kind"] != actual_state[key]
    }
    assert not wrong, f"登记的容器类型与实际不符：{wrong}"


# ── S-2 / S-3 ─────────────────────────────────────────────────────────────
def test_every_container_has_a_reclaim_api() -> None:
    missing = sorted(k for k, v in REGISTRY.items() if not v["reclaim"])
    assert not missing, (
        f"这些共享状态没有任何回收 API（S-2）：{missing}\n"
        "        没有回收 API 的容器只能靠「每个测试用独立 session id」侥幸隔离。"
    )


def test_reclaim_api_is_not_a_production_side_test_hook() -> None:
    """S-3：生产包不得为测试开后门。`*_for_tests` 钩子应移出生产（MUST 逐项处理）。"""
    offenders = sorted(
        f"{key}::{meta['reclaim']}"
        for key, meta in REGISTRY.items()
        if meta["reclaim"].endswith("_for_tests")
    )
    assert not offenders, (
        f"回收 API 是生产侧的测试钩子（S-3）：{offenders}\n"
        "        测试专用的重置逻辑 MUST 移出生产包（放进测试支撑目录），"
        "生产侧只保留语义化的回收 API。"
    )


# ── S-4 ───────────────────────────────────────────────────────────────────
def test_tests_never_write_production_shared_state() -> None:
    """测试 MUST NOT 直写生产私有共享状态（读断言允许）。

    例外：`tests/_support/` 是集中式支撑缝（T053）——对生产私有容器的
    写入 MUST 收敛在该目录内，其余测试经缝的具名函数或生产语义化回收
    API 操作容器。豁免是有意显式的；缝自身的锚定由
    `test_seam_is_anchored_to_registry_containers` 防空转。

    已知盲区（本轮不扩大口径，留待后续任务）：经局部变量别名的写入
    （`table = mod._X; table.clear()`、`mod._registry(sid)[k] = v`）
    检测不到——末段比对只认点分名。`tests/unit/probe_isolation.py`
    的 `_reset()` 属此类。
    """
    names = {key.split("::", 1)[1] for key in REGISTRY}
    offenders: list[str] = []
    for py in _iter_source_files(_TESTS):
        if _SEAM in py.parents:
            continue  # 支撑缝豁免：唯一直写点（防掏空判据见下方独立测试）
        rel = py.relative_to(_PKG).as_posix()
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            hit: str | None = None
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                cand = _dotted(node.value).split(".")[-1]
                # 必须与登记名比对：少了这一步，任何 `foo[k] = v` 都会被当成
                # "直写生产共享状态"（实测误报 146 处，全是 counts/actions 这类局部名）。
                hit = cand if cand in names else None
            elif isinstance(node, ast.Delete):
                for t in node.targets:
                    cand = _dotted(t).split(".")[-1]
                    if cand in names:
                        hit = cand
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _MUTATORS
            ):
                cand = _dotted(node.func.value).split(".")[-1]
                if cand in names:
                    hit = cand
            if hit:
                offenders.append(f"{rel}:{node.lineno} 写入 {hit}")
    assert not offenders, (
        "测试直写生产共享状态（S-4）——判据因此与执行顺序耦合：\n  - "
        + "\n  - ".join(offenders[:20])
        + (f"\n  …（共 {len(offenders)} 处）" if len(offenders) > 20 else "")
        + "\n        对生产私有容器的写入 MUST 走 tests/_support/state.py 的具名函数，"
        "或生产语义化回收 API（reset_value_sessions / drop_session 等）。"
    )


def test_seam_exists_and_is_imported_by_tests() -> None:
    """豁免缝 MUST 真实存在且被至少一个测试引用（防豁免空转）。

    豁免目录若成孤儿，S-4 的豁免就退化为无锚点的永久后门：缝被删 →
    豁免对象不存在；无人经缝操作容器 → 直写收敛形同虚设（21 处全走
    生产 API 是合法终态，但缝的存在与被引用是豁免的锚定前提）。
    两阶段锚定：缝文件存在 + tests/ 下（缝自身除外）至少一个文件
    import 缝。

    注意"缝内每个登记容器都必须有直写"是**过强**判据（会误红）：
    `_VALUE_SESSIONS` / `_SNAPSHOTS` / `_exposed_registry` / `_module_cache`
    的测试需求已被生产语义化回收 API 覆盖，无须经缝。
    """
    state_py = _SEAM / "state.py"
    assert state_py.exists(), (
        "支撑缝缺失：S-4 的豁免失去了唯一锚点——恢复 tests/_support/state.py 或同步收窄豁免"
    )
    importers: list[str] = []
    for py in _iter_source_files(_TESTS):
        if _SEAM in py.parents:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                node.module == "_support" or (node.module or "").startswith("_support.")
            ):
                importers.append(py.name)
                break
            if isinstance(node, ast.Import) and any(
                a.name == "_support" or a.name.startswith("_support.") for a in node.names
            ):
                importers.append(py.name)
                break
    assert importers, (
        "无任何测试引用支撑缝：直写收敛形同虚设，S-4 的豁免失去锚点——"
        "恢复经 tests/_support/state.py 的具名函数操作容器，或同步收窄豁免"
    )


# ── S-5 ───────────────────────────────────────────────────────────────────
def test_reclaim_api_actually_empties_the_container() -> None:
    """功能判据：登记的回收 API 必须真的把容器清空（否则"有 API"只是空话）。"""
    from ppsspp_dfx_mcp.service import probe_observer  # noqa: PLC0415 — 需要可导入的包

    # T053 S-4：种入经支撑缝，本判据不再自身直写生产私有容器。
    state_seam.seed_probe_registry("spec010-probe", {"0x1000": object()})
    state_seam.seed_probe_seeded("spec010-probe")
    assert probe_observer._REGISTRY_BY_SESSION, "前置：容器应已被写入"

    probe_observer.drop_session("spec010-probe")

    assert "spec010-probe" not in probe_observer._REGISTRY_BY_SESSION, (
        "drop_session 未清空观察者注册表"
    )
    assert "spec010-probe" not in probe_observer._SEEDED_BY_SESSION, "drop_session 未清空探测种子集"


def test_dropping_an_unknown_session_is_a_no_op() -> None:
    """回收 API MUST 容忍不存在的 session（否则重置顺序会变成新的耦合源）。"""
    from ppsspp_dfx_mcp.service import probe_observer  # noqa: PLC0415

    probe_observer.drop_session("spec010-never-existed")  # MUST NOT raise
