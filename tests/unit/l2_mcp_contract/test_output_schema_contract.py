"""test_output_schema_contract.py — L2 契约：工具输出 schema 结构化守卫。

Anchor: openspec change `ppsspp-dfx-mcp-protocol-and-schema`
- `specs/tool-schema-contract/spec.md`（ADDED 四条 requirement）
- `design.md` D2（TypedDict 方案）

本文件承担两个职责：

1. **形态守卫（全局）**：每个注册工具的 `outputSchema` 不得是
   `{"type":"object","additionalProperties":true}` 这类"无契约"形态；数组返回的
   `items` 不得为空 schema。**这是 `tool-schema-contract` 的可执行化**——
   治理推进过程中它逐步转绿，Group 3 完成的标志就是它全绿。

2. **契约一致性**：工具的 TypedDict 输出标注必须与对应 view 的字段集**逐一相同**。
   两处定义同一件事，漂移是必然的（本文件首次运行时即抓到一例：手工为
   `DisassemblyOutput` 多写了一个不存在的 `text` 字段）。

**为什么守卫要遍历"全部注册工具"而非抽查**：`additionalProperties: true` 的存活
方式就是**不被看见**。只抽查的守卫会放过它——本仓库已有先例（`imagecontent-output`
主 spec 因 delta 头而 11 条 requirement 对解析器不可见，同类问题）。
"""

from __future__ import annotations

from typing import Any, TypedDict

import pytest

from ppsspp_dfx_mcp.tools._common import DYNAMIC_INPUT_PARAMETERS


def _registered_tools() -> list[Any]:
    """触发全量注册并返回工具对象列表。"""
    from ppsspp_dfx_mcp import server as S

    S.register_all_tools()
    return S.mcp._tool_manager.list_tools()


def _detect_multi_shape_tools() -> dict[str, list[str]]:
    """AST 检测：同一工具函数构造 ≥2 个 `*Response` 类的工具。

    返回 `{tool_name: [ResponseClass, ...]}`。

    为什么要检测而不是靠 review：多形态工具的契约 bug **只在被调用的那个分支上
    暴露**。本仓的 `ppsspp_session(action='wait_ready')` /
    `ppsspp_batch_step(background=true)` 在补 outputSchema 的那次变更里双双变成
    运行时硬失败，而 1448 个测试**全绿**——因为没有一个测试走那两个分支。
    这个检测器把"该分支会在真实调用时报错"提前成红灯。

    检测器本身也不是万能的（构造成员的两种写法都要覆盖）：只认 `.from_result`
    会漏掉 `XResponse(...)` 构造式，只认构造式会漏掉工厂式。两种都收集。
    """
    import ast
    import inspect
    import textwrap

    found: dict[str, list[str]] = {}
    for tool in _registered_tools():
        fn = getattr(tool, "fn", None)
        if fn is None:
            continue
        try:
            src = textwrap.dedent(inspect.getsource(fn))
            tree = ast.parse(src)
        except (OSError, TypeError, SyntaxError):            continue
        classes: set[str] = set()
        for node in ast.walk(tree):
            # 构造式：XResponse(...)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id.endswith("Response")
            ):
                classes.add(node.func.id)
            # 工厂式：XResponse.from_result(...)
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id.endswith("Response")
            ):
                classes.add(node.value.id)
        if len(classes) > 1:
            found[tool.name] = sorted(classes)
    return found


# ============================================================================
# 1b. 运行时校验闭环 —— 契约不只是元数据，它开启了返回值校验
# ============================================================================


class TestMultiShapeToolsAreRegistered:
    """多形态工具必须登记为 `partial` 契约。

    背景见 `views/_contract.py` 的模块 docstring：SDK 在 `output_schema is not
    None` 时会对返回值执行 `validate_python`，**缺少 required 字段即工具报错**。
    一个工具按分支返回不同 view 时，单形态契约必然在部分分支上失败。

    本组的三条断言合起来构成闭环：检测 → 登记 → 契约确实放宽。
    """

    def test_detected_multi_shape_tools_are_all_registered(self):
        detected = _detect_multi_shape_tools()
        from ppsspp_dfx_mcp.tools._common import MULTI_SHAPE_OUTPUT_TOOLS

        missing = sorted(set(detected) - set(MULTI_SHAPE_OUTPUT_TOOLS))
        assert not missing, (
            "检测到未登记的多形态工具：\n  "
            + "\n  ".join(f"{n}: {detected[n]}" for n in missing)
            + "\n修复：把它们的契约改为 derive_output_contract(..., partial=True)，"
            "并在 tools/_common.MULTI_SHAPE_OUTPUT_TOOLS 登记理由。"
            "否则这些工具的某些分支会在真实调用时报 ValidationError。"
        )

    def test_registry_has_no_stale_entries(self):
        from ppsspp_dfx_mcp.tools._common import MULTI_SHAPE_OUTPUT_TOOLS

        detected = set(_detect_multi_shape_tools())
        stale = sorted(set(MULTI_SHAPE_OUTPUT_TOOLS) - detected)
        assert not stale, (
            f"登记为多形态但已检测不到：{stale} — 重构成单形态后请删除登记，"
            f"否则该工具会一直用放宽的契约（丢掉 required 信息）"
        )

    def test_registered_multi_shape_tools_declare_no_required_fields(self):
        """登记还不够——契约本身必须真的放宽（`total=False`）。

        只看 wire 层：`partial=True` 的 ToolsDict 生成的 schema 不带 `required`。
        """
        from ppsspp_dfx_mcp.tools._common import MULTI_SHAPE_OUTPUT_TOOLS

        by_name = {t.name: t for t in _registered_tools()}
        offenders: list[str] = []
        for name in sorted(MULTI_SHAPE_OUTPUT_TOOLS):
            tool = by_name.get(name)
            if tool is None:
                continue  # 未注册（如被条件禁用）——不属本断言范围
            required = (tool.output_schema or {}).get("required")
            if required:
                offenders.append(f"{name} 仍声明 required={sorted(required)[:6]}")
        assert not offenders, (
            "多形态工具的契约未放宽（会在非主分支上校验失败）：\n  " + "\n  ".join(offenders)
        )


class TestDynamicScriptToolContract:
    """动态脚本工具的契约必须描述**实际返回的信封**，而非脚本输出本身。

    这是一次真实事故的固化：首版把契约直接派生自脚本的 Output 模型，而包装器
    实际返回 `run_script()` 的 `{name, output, output_model}` 信封——SDK 校验
    必然失败，**每个**动态工具调用都报错。同一事故在测试套件里不可见，
    因为它只在校验路径上暴露。

    本仓的 3 个动态工具由 lifespan 注册，`register_all_tools()` 看不到它们，
    故此处断言契约的**构造方式**（信封三字段）而非具体实例。
    """

    def test_envelope_contract_shape(self):

        from ppsspp_dfx_mcp.views._contract import derive_output_contract
        from ppsspp_dfx_mcp.views.screenshot import TextureDumpResponse

        inner = derive_output_contract("_InnerProbe", TextureDumpResponse)
        # 动态 TypedDict：output 字段是运行时求值的 inner（镜像 server.py
        # 的动态 envelope 模式），类语法无法表达，故 noqa。
        envelope = TypedDict(  # type: ignore[operator]  # noqa: UP013
            "_EnvelopeProbe",
            {"name": str, "output": inner, "output_model": str},
        )
        assert set(envelope.__annotations__) == {"name", "output", "output_model"}

    def test_wrapper_source_uses_envelope_not_bare_output_model(self):
        """防止回退：包装器的契约必须把脚本输出嵌在 `output` 字段下。"""
        import ast
        import inspect
        import textwrap

        from ppsspp_dfx_mcp import server as S

        src = textwrap.dedent(inspect.getsource(S))
        tree = ast.parse(src)
        # 找 `output_contract = TypedDict(...)` 或 `derive_output_contract(...)`
        # 的赋值，断言其字面量含 output 键。
        found_envelope = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "output_contract" for t in node.targets
            ):
                text = ast.dump(node.value)
                if "'output'" in text and "'output_model'" in text:
                    found_envelope = True
        assert found_envelope, (
            "server._exposed_wrapper 的输出契约不再是 {name, output, output_model} "
            "信封——动态工具调用会因返回值不符契约而全部失败"
        )


# ============================================================================
# 形态判定工具函数
# ============================================================================


def _is_freeform(schema: dict[str, Any] | None) -> bool:
    """无契约形态：只有 `additionalProperties: true`，没有任何字段声明。"""
    if not isinstance(schema, dict):
        return False
    if schema.get("properties"):
        return False
    return schema.get("additionalProperties") is True


def _has_unconstrained_items(schema: dict[str, Any] | None) -> bool:
    """数组返回的 `items` 为空 schema（`{}` 或裸 `true`）。"""
    if not isinstance(schema, dict):
        return False
    for value in (schema.get("properties") or {}).values():
        if isinstance(value, dict) and value.get("type") == "array":
            items = value.get("items")
            if items is True or (isinstance(items, dict) and not items):
                return True
    return False


# ============================================================================
# 1. 形态守卫（全局）—— Group 3 完成的标志是它全绿
# ============================================================================


class TestOutputSchemaIsStructured:
    """`tool-schema-contract`: 工具 SHALL 声明结构化 outputSchema。"""

    def test_no_freeform_output_schema(self):
        """不存在"只声明 additionalProperties"的工具。

        该形态的语义是「返回任意内容」——不构成契约，Agent 无法据此判断返回字段。
        """
        offenders = sorted(t.name for t in _registered_tools() if _is_freeform(t.output_schema))
        assert not offenders, (
            f"{len(offenders)} 个工具的 outputSchema 无契约（additionalProperties 单键形态）：\n  "
            + "\n  ".join(offenders)
            + "\n修复：为该工具定义 TypedDict 输出标注（见 tools/memory.py 的 MemoryReadOutput 范式）"
        )

    def test_no_unconstrained_array_items(self):
        """不存在 `items: {}` 的数组 schema（`ppsspp_dump_clut` 的原始缺陷形态）。"""
        offenders = sorted(
            t.name for t in _registered_tools() if _has_unconstrained_items(t.output_schema)
        )
        assert not offenders, f"{len(offenders)} 个工具的数组 items 无约束：\n  " + "\n  ".join(
            offenders
        )


class TestOutputSchemaFieldsAreConstrained:
    """`tool-schema-contract`: **每个字段** SHALL 携带类型/枚举/`$ref` 约束。

    顶层非空不足够——本组断言的是「字段级」那一半。加它的直接触发因素是 MCP
    Inspector 的可移植性 linter：它把无关键字的字段报为
    "carries no validation keyword at all, so it accepts any value"，当场点出
    `ppsspp_read_memory.value` 与 `ppsspp_query.data` 两个 `Any` 字段——而先前的
    守卫只看顶层，放过了它们。**规格写了的就得能执行**，否则规格会先于实现腐化。

    `anyOf` / `oneOf` / `allOf` 计入合规：带类型分支的联合是真实的类型约束，
    正是「取值随兄弟字段变化的字段」应有的表达方式（见
    `views/_contract.py:derive_output_contract` 的 `overrides`）。
    """

    #: 任一即视为已声明约束。
    _KEYWORDS = ("type", "enum", "const", "$ref", "anyOf", "oneOf", "allOf")

    def test_every_output_field_carries_a_constraint(self):
        offenders: list[str] = []
        for tool in _registered_tools():
            for prop, spec in (tool.output_schema or {}).get("properties", {}).items():
                if not isinstance(spec, dict):
                    continue
                if not any(k in spec for k in self._KEYWORDS):
                    offenders.append(f"{tool.name}.{prop}")
        assert not offenders, (
            f"{len(offenders)} 个输出字段无任何约束关键字（空 schema 形态）：\n  "
            + "\n  ".join(sorted(offenders))
            + "\n修复：在 derive_output_contract(..., overrides={...}) 中声明该字段"
            "真实的联合类型，而不是留它作 Any"
        )


class TestInputSchemaIsStructured:
    """`tool-schema-contract`: 工具 SHALL 声明结构化 inputSchema。"""

    #: 有意使用自由形态的参数字段——其结构由**运行时数据**决定，静态无法约束。
    #: 清单来自 **src**（`tools/_common.DYNAMIC_INPUT_PARAMETERS`）而非本文件：
    #: 守门脚本 `scripts/report_schema_surface.py` 的退出码是 CI 信号，它必须
    #: 与守卫测试用同一份豁免——两份清单必然漂移（曾使该脚本永久退出 1）。
    #: 新增豁免须在 `tool-schema-contract` spec 的例外条款下论证。
    DYNAMIC_INPUT_FIELDS = DYNAMIC_INPUT_PARAMETERS

    def test_no_freeform_input_field(self):
        """参数字段不得为无约束的自由形态——动态结构参数除外（显式登记）。

        注意属性名差异：注册层 `mcpserver.tools.base.Tool` 用 `parameters`，
        wire 层 `mcp.types.Tool` 用 `input_schema`——本测试跑在注册层。
        """
        offenders: list[str] = []
        for tool in _registered_tools():
            for prop, spec in (tool.parameters.get("properties") or {}).items():
                if not isinstance(spec, dict):
                    continue
                if spec.get("additionalProperties") is True and not spec.get("properties"):
                    field = f"{tool.name}.{prop}"
                    if field not in self.DYNAMIC_INPUT_FIELDS:
                        offenders.append(field)
        assert not offenders, f"{len(offenders)} 个参数无类型约束：\n  " + "\n  ".join(
            sorted(offenders)
        )

    def test_exempted_dynamic_params_are_documented(self):
        """豁免不是"免检"——被豁免的参数必须在其 description 中说明动态性。

        防止豁免清单变成逃避治理的后门：登记豁免的同时必须解释来源。
        """
        for tool in _registered_tools():
            for prop, spec in (tool.parameters.get("properties") or {}).items():
                if f"{tool.name}.{prop}" not in self.DYNAMIC_INPUT_FIELDS:
                    continue
                desc = (spec.get("description") or "").lower()
                assert desc, f"{tool.name}.{prop} 被豁免但无 description"
                assert any(k in desc for k in ("json dict", "validated against")), (
                    f"{tool.name}.{prop} 被豁免但 description 未说明其形状来源：{desc[:120]!r}"
                )


# ============================================================================
# 2. 契约一致性 —— TypedDict 输出标注 vs 对应 view
# ============================================================================


#: (TypedDict 输出标注, 对应 Pydantic view, 被 exclude 掉的字段) 三元组。
#: **自动**收集：`derive_output_contract` 把来源 view 记在产物上
#: (`__contract_source_view__` / `__contract_excluded__`)，本函数遍历全部工具
#: 模块即可。前一版靠手写清单登记，结果是「登记清单从不被读、参数化用的是另一份
#: 硬编码列表、新增契约既不进登记也不被断言」——两份真相必然漂移。
def _collect_pairs() -> list[tuple[type, type, frozenset[str]]]:
    """扫描已导入的工具模块，取出全部派生契约及其来源 view。"""
    from ppsspp_dfx_mcp import server as S
    from ppsspp_dfx_mcp.tools import _common  # noqa: F401 — 确保包已导入

    S.register_all_tools()

    import sys

    pairs: dict[str, tuple[type, type, frozenset[str]]] = {}
    for mod_name, module in list(sys.modules.items()):
        if not mod_name.startswith("ppsspp_dfx_mcp"):
            continue
        for attr, value in vars(module).items():
            view = getattr(value, "__contract_source_view__", None)
            if view is None:
                continue
            pairs[f"{mod_name}.{attr}"] = (
                value,
                view,
                getattr(value, "__contract_excluded__", frozenset()),
            )
    return [pairs[k] for k in sorted(pairs)]


class TestOutputContractMatchesView:
    """派生契约的键集必须与来源 view 的字段集（减去 exclude）完全相同。

    两处定义同一结构，漂移必然发生（本文件首跑即抓到一例）。这组断言是
    `tool-schema-contract` 的「契约与实现同步」保证。
    """

    def test_registry_is_populated(self):
        """自动收集不得为空——空集会让下面的参数化静默变成 0 个用例。"""
        pairs = _collect_pairs()
        assert len(pairs) >= 30, f"只自动发现 {len(pairs)} 个派生契约，疑似扫描失效（应为 40 左右）"

    @pytest.mark.parametrize(
        "out_cls,view_cls,excluded",
        _collect_pairs(),
        ids=lambda x: getattr(x, "__name__", str(x)),
    )
    def test_view_descriptions_survive_into_the_contract(
        self, out_cls: type, view_cls: type, excluded: frozenset[str]
    ):
        """view 的字段说明必须进入契约（Agent 读的是 schema，不是 Python 源码）。

        `overrides` 是**替换**注解，直接赋值会把 `Field(description=...)` 一并
        抹掉——实测 `ppsspp_read_memory.value` 就是这样丢掉说明的。这条断言把
        「替换时保留说明」固化为契约。
        """

        missing: list[str] = []
        for field_name, annotation in out_cls.__annotations__.items():
            view_field = view_cls.model_fields.get(field_name)
            if view_field is None or not view_field.description:
                continue
            metadata = getattr(annotation, "__metadata__", ())
            if not any(getattr(m, "description", None) for m in metadata):
                missing.append(field_name)
        assert not missing, f"{out_cls.__name__} 中以下字段丢失了 view 的 description：{missing}"

    @pytest.mark.parametrize(
        "out_cls,view_cls,excluded",
        _collect_pairs(),
        ids=lambda x: getattr(x, "__name__", str(x)),
    )
    def test_keys_match_view_fields(self, out_cls: type, view_cls: type, excluded: frozenset[str]):
        out_keys = set(out_cls.__annotations__)
        view_keys = set(view_cls.model_fields) - set(excluded)
        assert out_keys == view_keys, (
            f"{out_cls.__name__} 与 {view_cls.__name__} 字段集不一致：\n"
            f"  仅 TypedDict 有: {sorted(out_keys - view_keys)}\n"
            f"  仅 view 有:      {sorted(view_keys - out_keys)}"
        )
