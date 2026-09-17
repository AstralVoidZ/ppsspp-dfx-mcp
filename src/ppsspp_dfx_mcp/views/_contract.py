"""views/_contract.py — 从 Pydantic view 派生工具函数的 TypedDict 输出契约。

**为什么需要**：MCP SDK 从工具函数的**返回类型标注**推导 `outputSchema`。工具实际
返回 `view.model_dump(mode="json")`（一个 dict）；若标注写 `dict[str, Any]`，SDK 只能
生成 `{"type": "object", "additionalProperties": true}`——语义是「返回任意内容」，
Agent 无法据此判断会收到哪些字段（openspec `tool-schema-contract` 所治理的形态）。

**为什么派生而非手写**：两处定义同一结构必然漂移。仓库首次运行输出契约守卫测试时
即抓到一例——手工为 `DisassemblyOutput` 多写了一个 view 中并不存在的 `text` 字段。
从 `model_fields` 派生则**结构性地不可能漂移**。

**本机制不是运行时中性的（重要）**：SDK 的 `func_metadata.convert_result()` 在
`output_schema is not None` 时会对返回值执行 `validate_python`
（`mcp/server/mcpserver/utilities/func_metadata.py:203-226`）。所以「补一个返回标注」
= **开启对该工具返回值的运行时校验**。校验边界（实测）：

| 情形 | 结果 |
|---|---|
| 字段齐全、类型匹配 | 通过 |
| 返回了契约未声明的**多余**字段 | **通过**（不 forbid extra） |
| **缺少**契约声明为 required 的字段 | **失败** → 工具报错 |
| 字段类型不在声明范围内 | **失败** → 工具报错 |

推论一：**一个工具若有多种返回形态**（按 action 分支返回不同 view），单形态契约
必然在部分分支上失败。此类工具必须用 `partial=True`（全字段可选）或显式联合契约。
`MultiShapeOutputRegistry`（见 `tools/_common.py`）登记全部此类工具，守卫测试
`test_multi_shape_tools_are_registered` 以 AST 检测强制登记——**漏登记的工具会在
真实调用时报错，而不是在测试里**。

推论二：**收紧字段类型同样有风险**（见 `overrides`）。声明一个过窄的联合会让
原本可用的返回变成硬失败，故 `int | str | ...` 这类联合要按**实测返回值**枚举，
而不是按「看起来合理」。

**为什么全部 required（单形态工具）**：`model_dump(mode="json")` 默认
`exclude_unset=False`，**总是输出全部字段**（含默认值）。故声明全字段必现才与实际
一致；若改用 Pydantic 模型作标注，SDK 会保留 `default`，反而暗示某些字段可能缺席
——那是误导。

**关于 `Any` 字段**：`value` 这类字段在 schema 中呈现为 `{"title": "Value"}`（无
`type`）。这是**字段级**的「任意类型」，与顶层 `additionalProperties: true` 的
「无契约」有本质区别——其余字段的类型仍然明确，Agent 仍能据此判断可读哪些字段。
但字段级 `Any` 同样不携带任何校验关键字，会被 MCP Inspector 的可移植性 linter 报为
"carries no validation keyword at all"，且违反 `tool-schema-contract` 的「每个字段
携带类型/枚举/$ref 约束」。**能枚举的联合类型就应当写出来**（用 `overrides`），
而不是留 `Any`。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, Field

__all__ = ["derive_output_contract"]


def derive_output_contract(
    name: str,
    view: type[BaseModel],
    *,
    exclude: frozenset[str] = frozenset(),
    overrides: Mapping[str, Any] | None = None,
    partial: bool = False,
) -> type:
    """从 view 派生 TypedDict，用作工具函数的返回类型标注。

    用法::

        from ppsspp_dfx_mcp.views._contract import derive_output_contract
        from ppsspp_dfx_mcp.views.step import StepResponse

        _StepOutput = derive_output_contract("StepOutput", StepResponse)

        @mcp.tool(name="ppsspp_step", ...)
        async def step(...) -> _StepOutput:
            ...
            return view.model_dump(mode="json")

    变化只发生在 SDK 推导出的 `outputSchema` **与它开启的返回值校验**上（见模块
    docstring）——工具返回的仍是 dict，Python 侧调用方无感。

    `exclude` 用于剔除**不进入结构化通道**的字段。典型场景是图像类工具：
    像素数据经 `content` 的 `ImageContent` 传递，`structuredContent` 只承载
    元数据——若把 `image_base64` 写进契约，既膨胀 schema 又与实际不符。

    **图像工具专用形态**：返回 `Annotated[CallToolResult, <本函数产物>]`。
    SDK 显式支持该组合（`func_metadata.py:416-420`）——它从 `Annotated` 的
    metadata 取输出模型来推导 schema，同时保留工具自建 `CallToolResult`
    （含 `ImageContent`）的能力。

    `overrides` 用于**收紧某个字段的声明类型**，而不动 view 本身。适用场景：
    view 的字段标注为 `Any`（因为取值随兄弟字段变化，用 Pydantic 联合类型会在
    `model_dump` 前的校验路径上开始拒绝真实数据），但其契约其实是**可枚举的**。
    此时在派生层给出真实的联合类型：schema 里出现 `anyOf` 分支，Agent 知道该
    字段是哪种形状。

    **注意**：`overrides` 打开的是**另一个方向的校验**——SDK 会用这个联合去校验
    真实返回值（见模块 docstring 的表格）。故联合必须覆盖**实测可能出现的全部
    类型**，宁可放宽（多一个 `list[str]` 分支）也不要漏，漏了就是硬失败。

    `overrides` 的键必须存在于 view 中（`exclude` 掉的字段也允许，以便收紧一个
    不进入结构化通道的字段的声明）。未知键**抛 `ValueError`**：拼写错误只可能
    来自手误，而静默丢弃会产出一个"契约里有、工具从不返回"的幽灵字段。

    `partial=True` 生成 `total=False`（全字段可选）。**多形态工具必须用它**——
    一个工具按 action 返回不同 view 时，单形态的 required 集合会在其他分支上
    校验失败（见模块 docstring 推论一）。
    """
    if overrides:
        unknown = sorted(set(overrides) - set(view.model_fields))
        if unknown:
            raise ValueError(
                f"{name}: overrides 含 view 中不存在的字段 {unknown}；"
                f"view {view.__name__} 的字段为 {sorted(view.model_fields)}"
            )
    hints: dict[str, Any] = {}
    for field_name, field in view.model_fields.items():
        if field_name in exclude:
            continue
        annotation = field.annotation
        hints[field_name] = Any if annotation is None else annotation
        # 保留 view 的字段说明：schema 里带 description 的输出契约对 Agent 的
        # 可用性有直接价值，而 Pydantic 的 Field 元数据在 TypedDict 里会丢失。
        if field.description:
            hints[field_name] = Annotated[hints[field_name], Field(description=field.description)]
    if overrides:
        for key, annotation in overrides.items():
            # 收紧类型不应丢掉 view 对该字段的说明——overrides 是替换注解，
            # 若直接赋值，`Annotated[..., Field(description=...)]` 会一并消失
            # （实测：ppsspp_read_memory.value 的说明曾因此丢失）。
            desc = view.model_fields[key].description
            if desc:
                annotation = Annotated[annotation, Field(description=desc)]
            hints[key] = annotation
    contract = TypedDict(name, hints, total=not partial)  # type: ignore[operator]
    # 记录出处，使「契约 ↔ view 字段集一致」可被**自动**校验：守卫测试遍历所有
    # 工具模块，凡是带 `__contract_source_view__` 的契约都在断言范围内，无需再
    # 手写一份登记清单（手写清单本身就是第二份真相，且已经出现过"登记了却从不
    # 被读"的情况）。
    contract.__contract_source_view__ = view  # type: ignore[attr-defined]
    contract.__contract_excluded__ = frozenset(exclude)  # type: ignore[attr-defined]
    return contract
