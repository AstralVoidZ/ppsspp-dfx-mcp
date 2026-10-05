"""opencode `--format json` 事件流解析（宽容式）。

对标 `porpoless/parse/opencode_events.py`，并按本采集线的实际需要扩展两点：
**工具调用提取**与**工具名归一**。

上游事件结构未文档化且随版本漂移（porpoless 实测基线 v1.18.30/1.18.31，本机
v2.0.19），故解析策略：按行/整体尽力 JSON 解析 → 递归收集 → 解析不出即空，由
调用方判败。

## 为什么必须做工具名归一

opencode 把 MCP 工具暴露为 `<serverKey>_<toolName>`。`.opencode/mcp.json` 的
server key 是 `ppsspp-dfx`，因此事件流里的名字形如
`ppsspp-dfx_ppsspp_health`——**不是** `ppsspp_health`。

旧 `opencode_collect.py` 用 `name.startswith("ppsspp_")` 过滤，该前缀在 v2
事件流上永不成立，于是 `parse_tool_calls` 恒返回空列表：实测
`runs-oc-20260929.jsonl` / `runs-oc-20260930.jsonl` 的 CTL-01 均为
`calls=0`、`--dump-events` 落盘 0 字节，`success=False`。这是**解析层缺陷，
不是模型答错**。归一后裸名与 `gates.py` 的期望对齐（门禁读 `c["name"]`
匹配 `ppsspp_*`）。

## 诊断通道

除目标工具外，`ParsedStream.other_tool_calls` 保留**全部**其他工具调用。
「一个目标调用都没有」时，这份清单是判断「agent 压根没调工具」还是
「工具名/结构变了」的唯一依据——即上一节缺陷当初本可被一眼看出的信息。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from evals.oc.log import get_logger

_log = get_logger("evals.oc.events")

#: 本采集线的目标工具前缀（归一后比对）
DEFAULT_TOOL_PREFIXES = ("ppsspp_",)

#: `.opencode/mcp.json` 里配置的 MCP server key——归一时剥掉的前缀
DEFAULT_SERVER_KEYS = ("ppsspp-dfx",)

# 非模型输出的 part 类型：错误 / 步骤 / 工具 / 推理，以及工具结果的**展示副本**
# （porpoless 实测 `read` 的 metadata.display.type == "file"，其 text 为被读
# 文件全文）。漏掉任一条都会让 final_answer 以文件内容开头。
_BAD_PART_TYPES = ("error", "step", "tool", "reasoning", "file", "display")

# usage 聚合语义（porpoless 实测）：上游各 token 字段**均为「每步」值**——
#   - 可加量（input/output）：跨步累加；
#   - total：取最大值（各步上下文大小，相加无意义）。
_MAX_USAGE_KEYS = frozenset({"total_tokens"})

_USAGE_ALIASES = (
    ("input", "input_tokens"),
    ("inputTokens", "input_tokens"),
    ("prompt_tokens", "input_tokens"),
    ("output", "output_tokens"),
    ("outputTokens", "output_tokens"),
    ("completion_tokens", "output_tokens"),
    ("totalTokens", "total_tokens"),
    ("total", "total_tokens"),
)

_RESULT_PREVIEW_CHARS = 500


@dataclass
class ToolCall:
    """一次工具调用。

    字段名与 `gates.py` 的读取口径、`runner.py` 的 JSONL 契约**逐字一致**——
    两通道轨迹必须可合并对账（设计 D3），故此处不做字段改名或重排。
    """

    seq: int
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    error_code: str | None = None
    result_preview: str = ""
    latency_ms: int = 0
    #: opencode 事件流里的原始名（未剥 server 前缀），仅供诊断
    raw_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "name": self.name,
            "args": self.args,
            "is_error": self.is_error,
            "error_code": self.error_code,
            "result_preview": self.result_preview,
            "latency_ms": self.latency_ms,
        }


@dataclass
class StreamError:
    """事件流里的结构化错误（opencode v2 实测形态）。"""

    type: str
    message: str
    status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "message": self.message, "status": self.status}


@dataclass
class ParsedStream:
    """一次 `opencode run` 事件流的解析结果。"""

    text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)
    other_tool_calls: list[ToolCall] = field(default_factory=list)
    errors: list[StreamError] = field(default_factory=list)
    object_count: int = 0

    @property
    def has_output(self) -> bool:
        """是否产出任何可解析内容（区分「空输出」与「有事件但无文本」）。"""
        return bool(
            self.text
            or self.tool_calls
            or self.other_tool_calls
            or self.errors
            or self.object_count
        )


def _collect_error(node: Any, out: list[StreamError]) -> None:
    """抓 `{"type": "error", "error": {...}}` 事件。

    v2 实测：provider 认证/额度错误只出现在这里（`provider.auth` +
    `"OpenCode's free tier can only be used from within OpenCode"`, status 403），
    **进程同时以非零码退出**。若不单独解析，`classify()` 只能按 returncode 猜，
    会把额度问题显示成 server 失联。
    """
    if isinstance(node, dict):
        if str(node.get("type", "")).lower() == "error":
            err = node.get("error")
            if isinstance(err, dict):
                out.append(
                    StreamError(
                        type=str(err.get("type") or "unknown"),
                        message=str(err.get("message") or "")[:500],
                        status=err.get("status") if isinstance(err.get("status"), int) else None,
                    )
                )
            elif isinstance(err, str):
                out.append(StreamError(type="unknown", message=err[:500]))
        for key, value in node.items():
            if key == "metadata":
                continue
            _collect_error(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_error(item, out)


def classify_stream_error(err: StreamError) -> str:
    """把结构化 error 映射为 `error_kind`（需从 errors 导入以避免循环依赖）。

    顺序有意义：**计费/额度类先判**，再判认证，再判限流——三者都含
    "quota"/"limit" 等词，先判限流会把「账号没钱了」显示成「等窗口即可」，
    导致反复重试一个永远不会自愈的故障。
    """
    from evals.oc.errors import ErrorKind

    kind = str(err.type).lower()
    blob = f"{err.type} {err.message}".lower()
    if err.status == 402 or "insufficient" in blob or "balance" in blob or "billing" in blob:
        return ErrorKind.PROVIDER_QUOTA
    if "auth" in kind or "auth" in blob or "credential" in blob or err.status in (401, 403):
        return ErrorKind.PROVIDER_AUTH
    if "429" in blob or "rate limit" in blob or "too many requests" in blob or "quota" in blob:
        return ErrorKind.RATE_LIMITED
    if "model" in blob and (
        "not found" in blob or "unavailable" in blob or "unknown" in blob or "invalid" in blob
    ):
        return ErrorKind.MODEL_UNAVAILABLE
    return ErrorKind.STREAM_ERROR


def _iter_json_objects(raw: str) -> list[Any]:
    """尽力解析：先整体解析，失败则按行解析（跳过非 JSON 行）。"""
    if not raw or not raw.strip():
        return []
    try:
        return [json.loads(raw)]
    except ValueError:
        pass
    objects: list[Any] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith(("{", "[")):
            continue
        try:
            objects.append(json.loads(line))
        except ValueError:
            continue
    return objects


def normalize_tool_name(raw: str, server_keys: tuple[str, ...] = DEFAULT_SERVER_KEYS) -> str:
    """剥掉 MCP server 前缀，归一为裸工具名。

    实测两种命名都存在，**分隔符不同**：

    | 版本 | 形态 |
    |---|---|
    | v1.x | `ppsspp-dfx_ppsspp_health`（下划线） |
    | v2.0.19 | `ppsspp-dfx.ppsspp_health`（**点号**） |

    v2 的点号形态来自本机 `opencode.json` 的 `mcp.servers` 键（`ppsspp-dfx`）
    与工具名 `ppsspp_health` 的直接拼接。**只按下划线剥前缀会在 v2 上 100% 漏掉
    全部 MCP 调用**——与「工具没注册」在轨迹上表现完全一样（`tool_calls=0`），
    却是完全相反的根因，排查成本极高。故两种分隔符都必须处理。

    只剥**已知** server key，避免误伤：工具名本身以下划线分段，任何「首个分隔符
    前缀」式截断都可能切错真实工具名（如 `other.ppsspp_health` 不是我们的）。
    """
    if not raw:
        return raw
    for key in server_keys:
        for sep in ("_", "."):
            prefix = f"{key}{sep}"
            if raw.startswith(prefix):
                return raw[len(prefix) :]
    return raw


#: opencode v2 把所有工具调用（含 MCP）包在 `execute` 沙箱里，实测形态：
#:
#: ```json
#: {"type":"tool_use","part":{"tool":"execute","state":{
#:   "input":{"code":"const r = await tools[\"ppsspp-dfx\"].ppsspp_health({});\nreturn r;"},
#:   "output":{"status":"ok", ...}}}}}
#: ```
#:
#: 工具路径形如 `tools["<serverKey>"].<toolName>`，可用 `search({query})` 查全量。
#: **因此 `execute` 事件是唯一能看到 ppsspp 工具调用的地方**——按 part.tool 过滤
#: （旧实现只看 `ppsspp-*` 前缀）在 v2 上必然 0 命中。必须把 code 展开。
_EXECUTE_TOOL_NAMES = frozenset({"execute", "run", "bash"})

#: `tools["server"].tool(` ——server 名与工具名
_MCP_CALL_RE = re.compile(
    r"""tools\s*\[\s*["']([^"']+)["']\s*\]\s*\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(""",
    re.VERBOSE,
)


def _balanced_args(code: str, open_paren: int) -> str:
    """从 `(` 起做括号配平扫描，取出实参文本（考虑字符串/转义/嵌套）。"""
    depth = 0
    i = open_paren
    quote = ""
    n = len(code)
    while i < n:
        ch = code[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return code[open_paren + 1 : i]
        i += 1
    return code[open_paren + 1 :]


def _parse_js_args(raw: str) -> dict[str, Any]:
    """把 JS 实参文本解析成 dict。

    模型通常写 JSON 形态对象字面量，故先按 JSON 试；失败则回退成
    `{"_raw": <原文>}`——保底让门禁的 `params` 能报告"参数形态异常"，
    而不是静默变成 `{}`。
    """
    text = raw.strip()
    if not text:
        return {}
    if text in ("{}", ""):
        return {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return {"_value": parsed}
    except ValueError:
        pass
    # JS 常见差异：单引号、无引号键、尾逗号 —— 逐条规范化后再试
    normalized = re.sub(r",(\s*[}\]])", r"\1", text)
    normalized = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)", r'\1"\2"\3', normalized)
    normalized = normalized.replace("'", '"')
    try:
        parsed = json.loads(normalized)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    return {"_raw": text[:400]}


def expand_execute_calls(
    code: str,
    result_text: str,
    *,
    server_keys: tuple[str, ...],
    tool_prefixes: tuple[str, ...],
    start_seq: int = 1,
) -> list[ToolCall]:
    """把一段 `execute` 代码里的 MCP 调用展开成 ToolCall 列表。

    非 MCP 的 `execute`（纯本地计算）返回空列表，调用方据此决定是否把这条
    `execute` 本身记进诊断通道。
    """
    out: list[ToolCall] = []
    for m in _MCP_CALL_RE.finditer(code or ""):
        server, tool = m.group(1), m.group(2)
        if server_keys and server not in server_keys:
            continue
        raw_args = _balanced_args(code, m.end() - 1)
        out.append(
            ToolCall(
                seq=start_seq + len(out),
                name=normalize_tool_name(f"{server}_{tool}", server_keys),
                args=_parse_js_args(raw_args),
                is_error=False,
                result_preview=result_text[:_RESULT_PREVIEW_CHARS],
            )
        )
    return [c for c in out if c.name.startswith(tool_prefixes)]


def _looks_like_text_part(node: dict) -> bool:
    kind = str(node.get("type", "")).lower()
    if any(bad in kind for bad in _BAD_PART_TYPES):
        return False
    return isinstance(node.get("text"), str)


def _looks_like_tool_part(node: dict) -> bool:
    """工具事件判定：type 含 tool 且带工具名。

    名字字段随版本漂移过（`name` / `tool` / `title`），故三选一。
    """
    kind = str(node.get("type", "")).lower()
    if "tool" not in kind:
        return False
    return any(isinstance(node.get(k), str) and node.get(k) for k in ("name", "tool", "title"))


def _as_text(value: Any) -> str:
    """结果值拍平成文本。

    工具结果既可能是 str，也可能是结构化 dict（v2 的 `state.output` 常为对象）；
    后者用 JSON 拍平，否则 `result_preview` 与 `error_code` 提取都会落空。
    """
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return "" if value is None else str(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _tool_payload(node: dict) -> tuple[str, dict, str, bool]:
    """从工具节点抽出 (name, args, result_text, is_error)。

    opencode v2.0.19 实测结构（**与 v1 差异显著**）：

    ```json
    {"type": "tool_use", "part": {"type": "tool", "tool": "execute",
      "state": {"status": "completed", "input": {...}, "output": "...", ...}}}
    ```

    注意 **args 在 `state.input` 里**，不在节点顶层。只读顶层的 `input`/`args`
    会让每一条调用的参数都变成 `{}`——而 `params` / `sequence` 门禁全部依赖
    参数，于是整张卡恒 fail 且原因难辨。两处都读，顶层优先。
    """
    name = ""
    for key in ("name", "tool", "title"):
        val = node.get(key)
        if isinstance(val, str) and val:
            name = val
            break
    state = node.get("state")
    state = state if isinstance(state, dict) else {}
    args: dict[str, Any] = {}
    for source in (state.get("input"), node.get("input"), node.get("args"), node.get("arguments")):
        if isinstance(source, dict) and source:
            args = source
            break
    is_error = bool(state.get("error") or state.get("is_error") or state.get("isError"))
    if not is_error and str(state.get("status", "")).lower() in ("error", "failed"):
        is_error = True
    # 出错时**优先**取 error 文本：v2 的失败事件里 `output` 往往还留着上一次
    # 成功的载荷，错误文本被它挤掉 → result_preview 与 error_code 提取双双落空。
    keys = (
        ("error", "output", "content", "text", "result")
        if is_error
        else (
            "output",
            "content",
            "text",
            "result",
            "error",
        )
    )
    result_text = ""
    for key in keys:
        result_text = _as_text(state.get(key))
        if result_text:
            break
    if not result_text:
        # 部分版本把结果平铺在节点自身（无 state 包装）
        for key in ("output", "result", "content"):
            result_text = _as_text(node.get(key))
            if result_text:
                break
    return name, args, result_text, is_error


def _collect_from_node(
    node: Any,
    texts: list[str],
    usage: dict[str, Any],
    ppsspp_calls: list[ToolCall],
    other_calls: list[ToolCall],
    server_keys: tuple[str, ...],
    tool_prefixes: tuple[str, ...],
    extract_error_code: Any,
) -> None:
    if isinstance(node, dict):
        if _looks_like_text_part(node):
            texts.append(node["text"])
        if _looks_like_tool_part(node):
            raw_name, args, result_text, is_error = _tool_payload(node)
            expanded: list[ToolCall] = []
            if raw_name in _EXECUTE_TOOL_NAMES and isinstance(args.get("code"), str):
                # execute 沙箱：MCP 调用藏在 code 里，展开它才看得见 ppsspp 工具
                expanded = expand_execute_calls(
                    args["code"],
                    result_text,
                    server_keys=server_keys,
                    tool_prefixes=tool_prefixes,
                    start_seq=len(ppsspp_calls) + 1,
                )
            if expanded:
                ppsspp_calls.extend(expanded)
                # execute 本身只是「运输工具」，不是被评测对象——不重复计入诊断通道
            else:
                norm = normalize_tool_name(raw_name, server_keys)
                bucket = ppsspp_calls if norm.startswith(tool_prefixes) else other_calls
                bucket.append(
                    ToolCall(
                        seq=len(ppsspp_calls) + len(other_calls) + 1,
                        name=norm,
                        args=args,
                        is_error=is_error,
                        error_code=(
                            extract_error_code(result_text)
                            if is_error and extract_error_code
                            else None
                        ),
                        result_preview=result_text[:_RESULT_PREVIEW_CHARS],
                        raw_name=raw_name,
                    )
                )
        for key in ("usage", "tokens", "cost"):
            sub = node.get(key)
            if isinstance(sub, dict):
                for src, dst in _USAGE_ALIASES:
                    val = sub.get(src)
                    if isinstance(val, (int, float)):
                        if dst in _MAX_USAGE_KEYS:
                            usage[dst] = max(usage.get(dst, val), val)
                        else:
                            usage[dst] = usage.get(dst, 0) + val
        for key, value in node.items():
            # 工具展示元数据不是模型输出——整棵子树排除。usage 挂在兄弟键 tokens
            # 上，不经 metadata，故该剪枝不影响用量采集。
            if key == "metadata":
                continue
            _collect_from_node(
                node=value,
                texts=texts,
                usage=usage,
                ppsspp_calls=ppsspp_calls,
                other_calls=other_calls,
                server_keys=server_keys,
                tool_prefixes=tool_prefixes,
                extract_error_code=extract_error_code,
            )
    elif isinstance(node, list):
        for item in node:
            _collect_from_node(
                node=item,
                texts=texts,
                usage=usage,
                ppsspp_calls=ppsspp_calls,
                other_calls=other_calls,
                server_keys=server_keys,
                tool_prefixes=tool_prefixes,
                extract_error_code=extract_error_code,
            )


def parse_stream(
    raw: str,
    *,
    server_keys: tuple[str, ...] = DEFAULT_SERVER_KEYS,
    tool_prefixes: tuple[str, ...] = DEFAULT_TOOL_PREFIXES,
    extract_error_code: Any = None,
) -> ParsedStream:
    """解析事件流原始输出。

    Args:
        raw: `opencode run --format json` 的 stdout 全文。
        server_keys: 归一时剥离的 MCP server key 前缀。
        tool_prefixes: 归一后判定「目标工具」的前缀。
        extract_error_code: `gates.extract_error_code`，用于给错误调用打上错误码。
            传 None 时 `error_code` 恒为 None（保底，避免解析层硬依赖门禁层）。

    Returns:
        `ParsedStream`——解析不出任何内容时各项为空，由调用方判败。
    """
    objects = _iter_json_objects(raw)
    texts: list[str] = []
    usage: dict[str, Any] = {}
    ppsspp_calls: list[ToolCall] = []
    other_calls: list[ToolCall] = []
    errors: list[StreamError] = []
    for obj in objects:
        _collect_from_node(
            node=obj,
            texts=texts,
            usage=usage,
            ppsspp_calls=ppsspp_calls,
            other_calls=other_calls,
            server_keys=server_keys,
            tool_prefixes=tool_prefixes,
            extract_error_code=extract_error_code,
        )
        _collect_error(obj, errors)
    # 目标工具单独重排 seq，保证 1..N 连续（门禁按 seq 语义读顺序）
    for i, call in enumerate(ppsspp_calls, start=1):
        call.seq = i
    if other_calls:
        _log.debug(
            "事件流含 %d 次非目标工具调用: %s",
            len(other_calls),
            ", ".join(sorted({c.raw_name or c.name for c in other_calls})[:10]),
        )
    if errors:
        _log.warning(
            "事件流含 %d 条 error 事件: %s",
            len(errors),
            "; ".join(f"{e.type}: {e.message[:120]}" for e in errors[:3]),
        )
    return ParsedStream(
        text="".join(texts),
        usage=usage,
        tool_calls=ppsspp_calls,
        other_tool_calls=other_calls,
        errors=errors,
        object_count=len(objects),
    )


def parse_tool_calls(raw: str, **kwargs: Any) -> list[dict[str, Any]]:
    """便捷入口：只要目标工具调用（JSONL 契约形态）。

    保留与旧 `opencode_collect.parse_tool_calls` 相同的返回类型，便于逐字对账。
    """
    return [c.to_dict() for c in parse_stream(raw, **kwargs).tool_calls]


def parse_final_answer(raw: str, **kwargs: Any) -> str:
    """便捷入口：只要模型最终文本输出。"""
    return parse_stream(raw, **kwargs).text
