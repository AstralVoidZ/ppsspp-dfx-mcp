"""spec/script_envelope.py — 动态脚本工具返回信封的单一构造点。

动态脚本工具（`ppsspp_script_<name>`）与 `ppsspp_run_script` 返回**同一个**
信封 `{name, output, output_model}`。历史上该形状在两处各写一遍：

- `server.py` 的 `_build_exposed_wrapper` 构造 TypedDict 返回契约；
- `tools/script.py` 的 `run_script` 构造实际返回 dict。

两处定义同一结构必然漂移，字段漏同步会直接表现为动态工具的输出校验失败
（SDK 会对返回 dict 执行 `validate_python`）。本模块是该形状的唯一来源。

两个函数：
- `build_envelope(name, output, output_model)` — 构造运行时返回 dict。
- `envelope_contract(name, output_cls)` — 构造 SDK 用来推导 `outputSchema`
  的 TypedDict 返回注解。信封名含**运行时** `name`，故类语法不可表达。
"""

from __future__ import annotations

from typing import Any, TypedDict

__all__ = ["build_envelope", "envelope_contract"]


def build_envelope(name: str, output: dict[str, Any], output_model: str) -> dict[str, Any]:
    """构造动态脚本 / `run_script` 的返回信封。

    Args:
        name: 脚本名（manifest 中的 `name`）。
        output: 脚本 Output 模型序列化后的 dict（`model_dump(mode="json")`）。
        output_model: Output 模型类名（供调用方做类型自省）。

    Returns:
        `{"name", "output", "output_model"}` —— 与 `envelope_contract` 声明
        的字段集逐一对应。
    """
    return {"name": name, "output": output, "output_model": output_model}


def envelope_contract(name: str, output_cls: type) -> type:
    """构造信封的 TypedDict 返回注解（字段与 `build_envelope` 一致）。

    `output_cls` 嵌套在信封的 `output` 字段下：这样脚本的字段级 schema 对
    Agent 仍可见，且与 `build_envelope` 的真实返回形状一致。

    Args:
        name: 脚本名；用于生成 `<name>OutputContract` 这一运行时类名。
        output_cls: 脚本 Output 的 Pydantic BaseModel 子类。

    Returns:
        一个运行时构造的 TypedDict，用作工具函数的返回类型标注。
    """
    # 动态 TypedDict：envelope 名含运行时 entry.name，类语法无法表达
    # （UP013 的类转换仅适用于静态定义），故本行 noqa。
    return TypedDict(  # type: ignore[operator]  # noqa: UP013
        f"{name}OutputContract",
        {
            "name": str,
            "output": output_cls,
            "output_model": str,
        },
    )
