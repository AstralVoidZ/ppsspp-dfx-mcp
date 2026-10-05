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
- 'func_add' — add HLE function tracking (name? / address?; size defaults to 4 — an omitted size creates an unusable zero-size function on PPSSPP <= v1.20.4-1845, and the response carries a verified flag)
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
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.core.registers import normalize_reg_name
from ppsspp_dfx_mcp.errors import ArgsInvalid, FuncNotFound, PpssppProtocolError, to_tool_error
from ppsspp_dfx_mcp.models.query import QueryResult
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import require_int_not_bool, translate_tool_errors
from ppsspp_dfx_mcp.views.query import QueryResponse

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    QueryOutput = dict[str, Any]
else:
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


# LONG-TOOL: one tool aggregates game_state / registers / threads / backtrace
# / HLE-function management and module listing behind a single action enum —
# splitting it would multiply the tool surface the baseline and docs pin.
@mcp.tool(
    name="ppsspp_query",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
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
                "Required for action='register' (the register name to "
                "read) and for action='func_add'. Ignored by func_remove "
                "because PPSSPP's hle.func.remove protocol does not "
                "accept a name parameter."
            ),
        ),
    ] = None,
    address: Annotated[
        str,
        Field(
            default="0x0",
            description=(
                "Required for func_remove and func_scan. Function address as a "
                "hex string (e.g. '0x08804000'). Not used by the other "
                "actions. The schema default of '0x0' exists for legacy "
                "callers -- do NOT rely on it when the action is one of the above."
            ),
        ),
    ] = "0x0",
    size: Annotated[
        int | None,
        Field(
            default=None,
            description=(
                "Function size in bytes, 'func_add' only. When omitted the "
                "server sends no size — on PPSSPP builds where the omit "
                "path underflows (v1.20.4-1845 and earlier) this produces "
                "an unusable zero-size function, so this tool defaults to "
                "sending 4. Pass an explicit size to override."
            ),
        ),
    ] = None,
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
    BEHAVIOR: READ-ONLY. Lookups only — func_add/func_remove mutate the debugger function list. Verified on a live game: threads / modules / funcs / func_scan respond while the CPU is RUNNING (no pause needed); running-state PC/isCurrent reads are LOW trust unless safe=true (which pauses briefly for a consistent, high-trust read).

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
    if top_n < 0:
        # A-8 (review v4): negative values used to take the same "no limit"
        # path as 0 and dump the 700+KB function table verbatim.
        raise ArgsInvalid(f"top_n must be >= 0 (0 = no limit), got {top_n}")

    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_query", "action": action, "session_id": session_id},
    )

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
                        # action='register' 的 name 非空由上面的早拒保证；
                        # 此断言仅向类型检查器传达该不变式。
                        assert name is not None
                        data = await client.get_reg(name=name, thread=thread)
                trust = "high"
            else:
                if action == "registers":
                    data = await client.get_all_regs()
                else:
                    # 同上：早拒保证 action='register' 必有 name。
                    assert name is not None
                    data = await client.get_reg(name=name, thread=thread)
                trust = "low"
            if action == "register":
                # G-3 (FR-003): cpu.getReg replies carry only the numeric
                # index (register=32), never the name. Echo the normalized
                # name — what PPSSPP actually looked up — so the caller can
                # self-verify that e.g. 'r8' was interpreted as 't0'.
                assert name is not None  # early reject above; typing only.
                data["name"] = normalize_reg_name(name)
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
            # ISS-008: PPSSPP returns the WHOLE symbol table regardless
            # of the requested range — filter to [address, address+size)
            # client-side so the requested window is what the caller
            # sees. total_before records the pre-filter size.
            if isinstance(data, dict) and isinstance(data.get("functions"), list):
                before = len(data["functions"])
                lo, hi = address_int, address_int + scan_size
                data["functions"] = [
                    f for f in data["functions"] if lo <= int(f.get("address", 0)) < hi
                ]
                data["filtered_to"] = f"0x{lo:08X}-0x{hi:08X}"
                data["total_before_filter"] = before
            # Truncate large function lists.
            if top_n > 0:
                data = _apply_top_n(data, top_n)
            result = QueryResult(action=action, data=data, trust_level=None)
        elif action == "func_add":
            addr = address_int if address_int != 0 else None
            # Always send an explicit size (default 4): omitting it
            # hits a zero-size underflow on PPSSPP builds up to
            # v1.20.4-1845 and yields an invisible, unremovable
            # function. Verify after the ack and surface the result.
            size_v = require_int_not_bool(size if size is not None else 4, "size")
            if size_v < 1:
                # A-3 (review v4): zero/negative sizes reproduce the
                # invisible, unremovable-function bug documented above.
                raise ArgsInvalid(
                    f"size must be >= 1 (got {size_v}) — a zero-size function "
                    f"is invisible and unremovable on PPSSPP <= v1.20.4-1845"
                )
            data = await client.func_add(name=name, address=addr, size=size_v)
            try:
                listing = await client.func_list()
                entries = listing.get("functions", []) if isinstance(listing, dict) else []
                if addr is None:
                    # A name-only func_add has no address to
                    # compare; `f.get("address") == (addr or 0)` actually
                    # asked "is there an address-0 entry?" — a false negative
                    # normally and a false positive if such an entry exists.
                    data["verified"] = any(f.get("name") == name for f in entries)
                else:
                    data["verified"] = any(f.get("address") == addr for f in entries)
                if not data["verified"]:
                    data["verified_note"] = (
                        "added function not visible in hle.func.list — "
                        "symbol map may not have accepted it"
                    )
            except Exception as verify_err:  # verification is best-effort
                data["verified"] = None
                data["verified_note"] = f"verify skipped: {verify_err}"
            result = QueryResult(action=action, data=data, trust_level=None)
        else:  # func_remove — address is guaranteed non-zero by the
            # validator above; name is not accepted by the protocol.
            try:
                data = await client.func_remove(address=address_int)
            except Exception as exc:
                # PPSSPP answers a remove for an unknown target with
                # "No function found at 'address'" — the parameter NAME is
                # printed in place of its value, and the event arrives as a
                # bare protocol error. Domain-ise it (FR-009) so the caller
                # can tell "no such tracked function" from a transport
                # failure, and gets the value that was actually attempted.
                translated = to_tool_error(exc)
                if isinstance(translated, PpssppProtocolError) and (
                    "no function found" in str(translated).lower()
                ):
                    raise FuncNotFound(
                        f"no tracked HLE function at 0x{address_int:08X} — "
                        f"run ppsspp_query(action='funcs') to list the tracked "
                        f"functions and retry with one of them"
                    ) from exc
                raise
            result = QueryResult(action=action, data=data, trust_level=None)
    return QueryResponse.from_result(result).model_dump(mode="json")


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
