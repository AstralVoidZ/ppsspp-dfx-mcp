"""views/_contract.py — 兼容 shim：输出契约编译器的历史导入点。

实现已迁至 `spec/output_contract.py`（契约编译器不属于工具层，也不需要 import
views —— 放进 spec 层后 `tools/` 与 `views/` 都能正向依赖它）。本模块保留原
`__all__` 与全部公开名，使历史导入点（~29 个工具模块、守卫测试）无需改动。
完整的设计说明（运行时校验边界、`partial`/`overrides`/`flatten_union` 的理由）
见 `spec/output_contract.py` 的模块 docstring。
"""

from __future__ import annotations

from ppsspp_dfx_mcp.spec.output_contract import (  # noqa: F401 — re-export shim
    derive_output_contract,
    flatten_union,
)

__all__ = ["derive_output_contract"]
