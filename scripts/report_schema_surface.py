#!/usr/bin/env python3
"""report_schema_surface.py — 工具面 JSON Schema 形态分布报告。

在一次 schema 治理变更前后对比 `inputSchema` / `outputSchema` 的形态分布，作为
「工具元数据契约」的**可复现基线**（openspec change
`ppsspp-dfx-mcp-protocol-and-schema` 的 tasks 1.3 / 3.10）。

判定口径与 `tool-schema-contract` spec 的 requirement 一一对应：

| 形态                  | 判定                                          | 对应 requirement            |
|-----------------------|-----------------------------------------------|-----------------------------|
| `structured`          | 顶层 `properties` 非空                        | 合规                        |
| `freeform-object`     | 仅 `{"type":"object","additionalProperties":true}` | 「自由形态不构成契约」  |
| `array-unconstrained` | `items` 为空 schema（`{}` 或裸 `true`）       | 「数组返回 SHALL 约束 items」|
| `missing`             | `outputSchema` 为 `None`                      | 「无结构化输出须显式声明」   |

**为什么不用 `dump_tool_surface.py`**：那个工具产出「工具面快照」（描述文本 +
schema 全文，用于基线 diff）；本工具产出「形态**分布**」（用于判断治理进度与
回归）。前者回答「变了什么」，后者回答「还差多少」。

用法::

    <venv python> scripts/report_schema_surface.py
    <venv python> scripts/report_schema_surface.py --json out.json

退出码：0 = 全部合规；1 = 存在不合规形态（可用于 CI/守门）。
输出一律 UTF-8。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from ppsspp_dfx_mcp.tools._common import DYNAMIC_INPUT_PARAMETERS


def _configure_stdout() -> None:
    """Windows 上 stdout 默认 cp936：`✓` / `—` 会退化成 `\\u2713` 字面量。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, OSError, ValueError):
        pass


def _is_empty_schema(node: Any) -> bool:
    """空 schema：`{}` 或裸 `true` —— 不携带任何校验关键字。"""
    return node is True or (isinstance(node, dict) and not node)


#: 字段级合规判据，与守卫测试
#: `TestOutputSchemaFieldsAreConstrained._KEYWORDS` 必须保持一致。
_FIELD_CONSTRAINT_KEYWORDS = ("type", "enum", "const", "$ref", "anyOf", "oneOf", "allOf")


def classify_output(schema: dict[str, Any] | None) -> str:
    """归类一个 outputSchema 的形态。"""
    if schema is None:
        return "missing"
    props = schema.get("properties") or {}
    if props:
        # 数组型：检查 items 是否受约束
        for value in props.values():
            if isinstance(value, dict) and value.get("type") == "array":
                if _is_empty_schema(value.get("items")):
                    return "array-unconstrained"
        # 字段级：每个字段至少要有一个校验关键字。只看顶层会让
        # `{"properties": {"value": {"title": "Value"}}}`（字段级 Any）被判为
        # structured —— 那正是 `ppsspp_read_memory.value` / `ppsspp_query.data`
        # 曾经的状态，也是 MCP Inspector 报 "carries no validation keyword at
        # all" 的形态。守门脚本与守卫测试必须用同一口径，否则退出码失去意义。
        for value in props.values():
            if not isinstance(value, dict):
                continue
            if not any(k in value for k in _FIELD_CONSTRAINT_KEYWORDS):
                return "field-unconstrained"
        return "structured"
    if schema.get("additionalProperties") is True:
        return "freeform-object"
    return "other"


def classify_input(schema: dict[str, Any] | None) -> str:
    """归类一个 inputSchema 的字段级形态。

    与 outputSchema 不同，inputSchema 顶层**总是** object + properties（工具参数
    模型），所以看的是**字段级**是否有自由形态。
    """
    if not schema:
        return "no-fields"
    props = schema.get("properties") or {}
    if not props:
        return "no-fields"
    for value in props.values():
        if not isinstance(value, dict):
            continue
        # 自由形态字段：显式 additionalProperties:true 且自身无 properties
        if value.get("additionalProperties") is True and not value.get("properties"):
            return "has-freeform-field"
        if _is_empty_schema(value) or ("type" not in value and "enum" not in value
                                       and "$ref" not in value and "anyOf" not in value
                                       and "oneOf" not in value and "allOf" not in value):
            return "has-unconstrained-field"
    return "structured"


def freeform_field(schema: dict[str, Any] | None) -> str | None:
    """第一个「显式自由形态」字段名（判定同 `classify_input` 的第一条规则）。

    返回值用于比对豁免清单 `DYNAMIC_INPUT_PARAMETERS`（键为
    `<tool>.<field>`）。只报第一个——多字段不合规时工具名仍会被列进不合规
    清单，不会漏。
    """
    if not schema:
        return None
    for name, value in (schema.get("properties") or {}).items():
        if not isinstance(value, dict):
            continue
        if value.get("additionalProperties") is True and not value.get("properties"):
            return name
    return None


async def collect() -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """启动 server 子进程，取回 capabilities 与全部工具。"""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ppsspp_dfx_mcp"],
        env={"PPSSPP_DFX_LOG_LEVEL": "ERROR"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            caps = init.capabilities.model_dump(exclude_none=True)
            tools = (await session.list_tools()).tools
            rows = [
                {
                    "name": t.name,
                    "input": classify_input(t.input_schema),
                    "output": classify_output(t.output_schema),
                    "freeform_field": freeform_field(t.input_schema),
                }
                for t in tools
            ]
            meta = {
                "protocol_version": getattr(init, "protocol_version", None),
                "tool_count": len(tools),
            }
            return caps, rows, meta


def main() -> int:
    _configure_stdout()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", type=Path, default=None,
                        help="把完整报告写入该路径（JSON）")
    args = parser.parse_args()

    caps, rows, meta = asyncio.run(collect())

    out_c = Counter(r["output"] for r in rows)
    in_c = Counter(r["input"] for r in rows)
    offenders = defaultdict(list)
    exempted: list[str] = []
    for r in rows:
        if r["output"] != "structured":
            offenders[f"output:{r['output']}"].append(r["name"])
        if r["input"] != "structured" and r["input"] != "no-fields":
            # Registered exceptions (spec `tool-schema-contract`, 动态结构参数
            # 例外条款) are reported separately and do NOT fail the gate —
            # the list is imported from src so this script and the guard test
            # can never disagree about what is compliant.
            if f"{r['name']}.{r['freeform_field']}" in DYNAMIC_INPUT_PARAMETERS:
                exempted.append(r["name"])
            else:
                offenders[f"input:{r['input']}"].append(r["name"])

    print("工具面 Schema 形态分布")
    print("─" * 62)
    print(f"协议版本    {meta['protocol_version']}")
    print(f"工具总数    {meta['tool_count']}")
    print()
    print("capabilities 声明:")
    for key in ("tools", "resources", "prompts", "completions", "logging", "tasks"):
        print(f"  {key:14} {'✓ ' + json.dumps(caps[key], ensure_ascii=False) if key in caps else '— 未声明'}")
    print()
    print("outputSchema 形态:")
    for shape in ("structured", "field-unconstrained", "freeform-object",
                  "array-unconstrained", "missing", "other"):
        if out_c.get(shape):
            print(f"  {shape:22} {out_c[shape]}")
    print()
    print("inputSchema 形态:")
    for shape in ("structured", "has-freeform-field", "has-unconstrained-field", "no-fields"):
        if in_c.get(shape):
            print(f"  {shape:22} {in_c[shape]}")

    if offenders:
        print()
        print("不合规清单（tool-schema-contract 口径）:")
        for kind in sorted(offenders):
            names = offenders[kind]
            print(f"  [{kind}] {len(names)} 个")
            for name in names[:8]:
                print(f"      {name}")
            if len(names) > 8:
                print(f"      … 另有 {len(names) - 8} 个")

    if exempted:
        print()
        print("已登记豁免（spec 的「动态结构参数」例外条款，不算不合规）:")
        for name in sorted(exempted):
            fields = sorted(
                f for f in DYNAMIC_INPUT_PARAMETERS if f.startswith(f"{name}.")
            )
            print(f"  {name}.{', '.join(f.split('.', 1)[1] for f in fields)}")

    if args.json:
        args.json.write_text(
            json.dumps({"meta": meta, "capabilities": caps, "tools": rows,
                        "distribution": {"output": dict(out_c), "input": dict(in_c)}},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
        print(f"\nJSON 报告: {args.json}")

    compliant = not offenders
    print()
    print("结论：" + ("工具面 schema 全部合规" if compliant else "存在不合规形态（见上）"))
    return 0 if compliant else 1


if __name__ == "__main__":
    raise SystemExit(main())
