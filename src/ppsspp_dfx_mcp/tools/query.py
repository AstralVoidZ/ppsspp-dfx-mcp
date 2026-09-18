"""Query tool wrappers.

2 tools exposed:
- ppsspp_query(action, ...) — aggregate query across game state, CPU
  registers, HLE backtrace, threads, modules, function tracking

Query actions (9 total):
- 'game_state' — PPSSPP game status (paused / running / game title)
- 'registers' — all CPU registers (GPR + FPU + VFPU)
- 'backtrace' — HLE call stack (thread optional)
- 'threads' — PSP thread list (safe: stepping → query → resume)
- 'modules' — list all loaded HLE modules
- 'funcs' — list registered HLE function tracking entries
- 'func_scan' — scan all trackable HLE functions
- 'func_add' — add HLE function tracking (name? / address?)
- 'func_remove' — remove HLE function tracking (address required;
  PPSSPP's hle.func.remove protocol only accepts `address`, no `name` —
  see HLESubscriber.cpp:L38, L319-363)

Async: uses session_client → PpssppDebugClient (composes WsTransport
+ SteppingManager) under the hood. Tools call DebugClient domain methods
(game_status / get_all_regs / backtrace / safe_get_threads / module_list /
func_list / func_scan / func_add / func_remove) directly; no orchestration
wrapper indirection.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError, to_tool_error
from ppsspp_dfx_mcp.models.query import QueryResult
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.query import QueryResponse

QueryOutput = derive_output_contract(
    "QueryOutput",
    QueryResponse,
    # `data` 在 view 里是 `Any`。**联合逐分支枚举**（本文件全部 `data=` 赋值点）：
    #   game_state/registers/register/backtrace/modules/funcs/func_add/func_remove
    #     → `debug_client` 对应方法均返回 `dict[str, Any]`
    #   threads → `ThreadSnapshot.threads`，其类型标注为 `list[dict]`
    # 用 `list[Any]` 会退化成空 `items`（违反 `tool-schema-contract` 的
    # 「数组返回 SHALL 约束 items」），故写具体元素类型。
    # 该联合会被 SDK 用于**运行时校验**，新增 action 时须同步扩这里。
    overrides={
        "data": dict[str, Any] | list[dict[str, Any]] | None,
    },
)

logger = logging.getLogger(__name__)

__all__ = ["query"]

_QUERY_ACTIONS: tuple[str, ...] = (
    "game_state",
    "registers",
    "register",
    "backtrace",
    "threads",
    "modules",
    "funcs",
    "func_scan",
    "func_add",
    "func_remove",
)


# Former docstring (kept as comment; description is now the TDQS docstring):
# Aggregate query tool.
#
# Action → required params:
# game_state → session_id
# registers  → session_id
# backtrace  → session_id (thread optional)
# threads    → session_id
# modules    → session_id
# funcs      → session_id
# func_scan  → session_id + address (scans 64KB range starting at address)
# func_add   → session_id (name? and/or address)
# func_remove→ session_id + address (PPSSPP protocol requires address;
# name is not accepted by hle.func.remove)
@mcp.tool(
    name="ppsspp_query",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
@translate_tool_errors
async def query(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    action: Annotated[
        Literal[
            "game_state",
            "registers",
            "register",
            "backtrace",
            "threads",
            "modules",
            "funcs",
            "func_scan",
            "func_add",
            "func_remove",
        ],
        Field(
            description=(
                "Query action. Valid values:\n"
                "- 'game_state': PPSSPP game status (paused / game title).\n"
                "- 'registers': all CPU registers (GPR + FPU + VFPU).\n"
                "- 'register': single register by name (MIPS ABI name like "
                "'a0'/'v0'/'t9', or 'pc'/'hi'/'lo').\n"
                "- 'backtrace': HLE call stack (thread optional).\n"
                "- 'threads': PSP thread list (safe: stepping → query → "
                "resume).\n"
                "- 'modules': list all loaded HLE modules.\n"
                "- 'funcs': list registered HLE function tracking entries.\n"
                "- 'func_scan': scan HLE functions in a 64KB range starting at "
                "address (requires address; CPU must be stepping).\n"
                "- 'func_add': add HLE function tracking (name? and/or "
                "address?).\n"
                "- 'func_remove': remove HLE function tracking (address "
                "required; PPSSPP protocol only accepts address, no name)."
            ),
        ),
    ],
    thread: Annotated[
        int | None,
        Field(
            default=None,
            description="Thread ID (backtrace action only; None = current).",
        ),
    ] = None,
    safe: Annotated[
        bool,
        Field(
            default=True,
            description=(
                "action=register/registers only: pause the CPU for a "
                "consistent read (trust_level='high', same as the retired "
                "ppsspp_get_pc) — or read without pausing "
                "(trust_level='low', zero cost, racy while running; "
                "for hot-path polling)."
            ),
        ),
    ] = True,
    name: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Function name (func_add only; ignored by func_remove "
                "because PPSSPP's hle.func.remove protocol does not "
                "accept a name parameter)."
            ),
        ),
    ] = None,
    address: Annotated[
        str,
        Field(
            default="0x0",
            description=(
                "Function address as a hex string (e.g. '0x08804000'). "
                "Required for func_remove and func_scan."
            ),
        ),
    ] = "0x0",
    top_n: Annotated[
        int,
        Field(
            default=100,
            description=(
                "Limit the number of entries returned for "
                "'funcs' / 'func_scan' actions (default 100). "
                "0 = no limit — hle.func.list can reach 700+KB, pass 0 "
                "only when the full list is genuinely needed."
            ),
        ),
    ] = 100,
) -> QueryOutput:
    """PURPOSE: Aggregate game-state queries — game_state, registers (all or one), backtrace, threads, modules, and function-list management (funcs/func_scan/func_add/func_remove).

    USAGE: action + session_id; 'register' needs name; func_scan/func_remove need address; top_n defaults to 100 (pass 0 for the full list — hle.func.list can reach 700+KB).


    ROUTING: one-shot PC read -> query(action='register', name='pc') (safe=true pauses for consistency; safe=false for hot-path polling); pause+capture -> ppsspp_frame_snapshot; recurring named probes -> ppsspp_state_observer; game_state / backtrace / threads / modules / HLE func management also here.
    BEHAVIOR: READ-ONLY. Lookups only — func_add/func_remove mutate the debugger function list. backtrace/threads/func_* REQUIRE the CPU paused; running-state PC/isCurrent reads are LOW trust unless safe=true.

    RETURNS: {action, data, trust_level} — data shape depends on the action."""
    if action not in _QUERY_ACTIONS:
        raise ArgsInvalid(f"invalid action={action!r}; expected one of {_QUERY_ACTIONS}")
    address_int = parse_address(address)
    if action == "func_add" and not name and address_int == 0:
        raise ArgsInvalid("action='func_add' requires at least one of name or address")
    if action == "func_remove" and address_int == 0:
        raise ArgsInvalid(
            "action='func_remove' requires a non-zero address "
            "(PPSSPP's hle.func.remove protocol does not accept a name)"
        )
    if action == "register" and not name:
        raise ArgsInvalid(
            "action='register' requires a register name "
            "(MIPS name like 'a0'/'v0'/'t9', or 'pc'/'hi'/'lo')"
        )
    if action == "func_scan" and address_int == 0:
        raise ArgsInvalid("action='func_scan' requires a non-zero address")

    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_query", "action": action, "session_id": session_id},
    )

    try:
        async with session_client(session_id) as client:
            if action == "game_state":
                data = await client.game_status()
                result = QueryResult(action=action, data=data, trust_level=None)
            elif action in ("registers", "register"):
                # safe=true (default) walks the with_stepping pause dance —
                # same semantics as the retired ppsspp_get_pc (trust HIGH).
                # safe=false is a raw read: zero pause cost, racy while
                # running (trust LOW) — the hot-path polling option.
                if safe:
                    async with client.with_stepping():
                        if action == "registers":
                            data = await client.get_all_regs()
                        else:
                            data = await client.get_reg(name=name, thread=thread)
                    trust = "high"
                else:
                    if action == "registers":
                        data = await client.get_all_regs()
                    else:
                        data = await client.get_reg(name=name, thread=thread)
                    trust = "low"
                result = QueryResult(action=action, data=data, trust_level=trust)
            elif action == "backtrace":
                data = await client.backtrace(thread=thread)
                result = QueryResult(action=action, data=data, trust_level=None)
            elif action == "threads":
                snapshot = await client.safe_get_threads()
                result = QueryResult(
                    action=action,
                    data=snapshot.threads,
                    trust_level=snapshot.trust_level,
                )
            elif action == "modules":
                data = await client.module_list()
                result = QueryResult(action=action, data=data, trust_level=None)
            elif action == "funcs":
                data = await client.func_list()
                # Truncate large function lists.
                if top_n > 0:
                    data = _apply_top_n(data, top_n)
                result = QueryResult(action=action, data=data, trust_level=None)
            elif action == "func_scan":
                # Default scan size: 64 KB. PPSSPP's hle.func.scan is
                # fire-and-forget (triggers scan, no business data returned).
                # Follow-up func_list to retrieve the scan results.
                scan_size = 65536
                await client.func_scan(address=address_int, size=scan_size)
                data = await client.func_list()
                # Truncate large function lists.
                if top_n > 0:
                    data = _apply_top_n(data, top_n)
                result = QueryResult(action=action, data=data, trust_level=None)
            elif action == "func_add":
                addr = address_int if address_int != 0 else None
                data = await client.func_add(name=name, address=addr)
                result = QueryResult(action=action, data=data, trust_level=None)
            else:  # func_remove — address is guaranteed non-zero by the
                # validator above; name is not accepted by the protocol.
                data = await client.func_remove(address=address_int)
                result = QueryResult(action=action, data=data, trust_level=None)
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e
    return QueryResponse.from_result(result).model_dump(mode="json")


# Former docstring (kept as comment; description is now the TDQS docstring):
# Safely read the current PC (stepping → query → resume).
#
# Returns:
# GetPcResponse dict: pc + trust_level.
#
# Raises:
# ToolError: on session lookup failure or WS failure.
# ppsspp_get_pc was absorbed into ppsspp_query:
# query(action='register', name='pc', safe=true) is the same read.


def _apply_top_n(data: Any, top_n: int) -> Any:
    """Truncate large lists in query data to top_n entries.

    Handles two shapes returned by PPSSPP's hle.func.list:
    - Plain list: ``[entry1, entry2, ...]`` → slice directly.
    - Dict with list values: ``{"functions": [...]}`` → truncate all
      list values that exceed top_n, preserving other keys.

    Non-list data is returned unchanged.
    """
    if top_n <= 0:
        return data
    if isinstance(data, list):
        return data[:top_n]
    if isinstance(data, dict):
        result = dict(data)
        for key, val in result.items():
            if isinstance(val, list) and len(val) > top_n:
                result[key] = val[:top_n]
        return result
    return data
