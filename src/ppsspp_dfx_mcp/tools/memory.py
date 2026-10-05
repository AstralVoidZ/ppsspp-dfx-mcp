"""Memory tool wrappers.

3 tools exposed:
- ppsspp_read_memory(action, ...) — aggregate read (bytes/u32/string)
- ppsspp_write_memory(address, data, format?) — write u32 or bytes
- ppsspp_disassemble(address, count?) — disassemble N instructions

Async: uses session_client → PpssppDebugClient (composed of WsTransport
+ SteppingManager) under the hood. Tools call DebugClient domain
methods (read_bytes / read_u32 / read_string / write_u32 /
write_bytes / disasm) directly; no orchestration wrapper indirection.
"""

from __future__ import annotations

import base64
import logging
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address, parse_value
from ppsspp_dfx_mcp.core.primitives import (
    MAX_SINGLE_READ_BYTES,
    MAX_WRITE_BYTES,
)
from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError
from ppsspp_dfx_mcp.models.memory import (
    DisassemblyResult,
    MemoryReadResult,
    MemoryWriteResult,
)
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.service.memory_protection import (
    check_protected_address,
    check_protected_address_static,
    resolve_session_modules,
)
from ppsspp_dfx_mcp.session.client_helper import resolve_session_id, session_client
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import (
    DEFAULT_STRING_CAP,
    require_int_not_bool,
    save_output_bytes,
    save_output_text,
    translate_tool_errors,
)
from ppsspp_dfx_mcp.views.memory import (
    DisassemblyResponse,
    MemoryReadResponse,
    MemoryWriteResponse,
)

logger = logging.getLogger(__name__)

__all__ = ["read_memory", "write_memory", "disassemble"]

_READ_ACTIONS = ("read_bytes", "read_u32", "read_string")

# 输出契约：从对应 view 派生（见 views/_contract.py 的机制说明）。
# 派生而非手写，使契约与实现**结构性地不可能漂移**——手写版本曾在首跑守卫
# 测试时即被抓到多写了一个不存在的字段。
# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    MemoryReadOutput = dict[str, Any]
    MemoryWriteOutput = dict[str, Any]
    DisassemblyOutput = dict[str, Any]
else:
    MemoryReadOutput = derive_output_contract(
        "MemoryReadOutput",
        MemoryReadResponse,
        # `value` 在 view 里是 `Any`：取值随 action 变化，用 Pydantic 联合类型会让
        # `model_dump` 前的校验开始拒绝真实数据。改在派生层声明真实联合。
        #
        # **联合不是猜的，是逐分支枚举的**（本文件全部 `value=` 赋值点）：
        #   L321 `value=list(raw)`      → list[int]      （read_bytes）
        #   L346/L369 `value=val`       → int / str      （read_u8/16/32 / read_string）
        #   L426 `value=matches`        → list[dict]     （scan；`scan_memory -> list[dict[str, Any]]`）
        #   L437/L439 `update={"value": None}` → None    （output="hex" / "file"）
        # 该联合会被 SDK 用于**运行时校验**（见 views/_contract.py 模块 docstring），
        # 故新增 action 或改变返回类型时必须同步扩这里，否则表现为工具报错。
        overrides={
            "value": int | str | list[int] | list[dict[str, Any]] | None,
        },
    )
    MemoryWriteOutput = derive_output_contract("MemoryWriteOutput", MemoryWriteResponse)
    DisassemblyOutput = derive_output_contract("DisassemblyOutput", DisassemblyResponse)


# Single-read cap — bounds response size and latency
# (1 MB took ~2.5s over the WS and cost unbounded tokens).
# single source of truth in tools/_common (was a duplicated literal)
_MAX_READ_BYTES = MAX_SINGLE_READ_BYTES

# Cap disassembly count to prevent 645KB+ outputs. PPSSPP's
# memory.disasm returns one dict per instruction; 100 instructions is
# ~50KB (well within MCP response limits). Callers requesting more get
# silently capped — the `count` field in the response reflects the
# actual number returned.
_MAX_DISASM_COUNT = 100

# Fields kept per instruction for output governance. PPSSPP's
# memory.disasm may return extra fields (encoding, branchDelay, etc.)
# that bloat the response. Keep only the essential fields.
_DISASM_KEEP_FIELDS = ("address", "text", "name", "params")


@mcp.tool(
    name="ppsspp_read_memory",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
@translate_tool_errors
async def read_memory(
    action: Annotated[
        Literal["read_bytes", "read_u32", "read_string"],
        Field(
            description=(
                "Read action. Valid values:\n"
                "- 'read_bytes': read raw bytes (requires address + size).\n"
                "- 'read_u32': read a 32-bit unsigned int (requires address).\n"
                "- 'read_string': read a string (requires address).\n"
            ),
        ),
    ],
    address: Annotated[
        str,
        Field(
            default="0x0",
            description=(
                "Required for every action. Starting address for "
                "read_bytes/read_u32/read_string, as a hex string "
                "(e.g. '0x08804000'). The schema default of '0x0' exists for "
                "legacy callers -- do NOT rely on it."
            ),
        ),
    ] = "0x0",
    size: Annotated[
        int,
        Field(
            default=0,
            description="Number of bytes to read (read_bytes only). Max "
            "65536 per call (MAX_SINGLE_READ_BYTES); larger reads are "
            "rejected with ARGS_INVALID -- chunk them instead.",
        ),
    ] = 0,
    output: Annotated[
        Literal["value", "hex", "file"],
        Field(
            default="value",
            description=(
                "Payload channel for read_bytes (ignored by other "
                "actions). 'value' (default) returns the byte list "
                "inline plus a hex dump in `text`. 'hex' keeps only the "
                "hex dump in `text` (value=null) — roughly half the "
                "characters. 'file' saves raw bytes + hex dump under "
                ".ppsspp-dfx/output/memory_reads/ and returns the paths "
                "plus a 64-byte preview — use for reads near the "
                "65536-byte cap."
            ),
        ),
    ] = "value",
    length: Annotated[
        int | None,
        Field(
            default=None,
            description="(deprecated, ignored) PPSSPP memory.readString does not "
            "accept a length parameter. Kept for backward schema compatibility.",
        ),
    ] = None,
    session_id: Annotated[
        str | None,
        Field(
            description=(
                "Active session ID; omit to auto-resolve when exactly one session is active."
            ),
        ),
    ] = None,
    max_len: Annotated[
        int,
        Field(
            default=0,
            description=(
                "Maximum string length in bytes for read_string "
                "(0 = default cap 4096). Values are clamped to 65536."
            ),
        ),
    ] = 0,
) -> MemoryReadOutput:
    """PURPOSE: Read memory (read_bytes / read_u32 / read_string).

    USAGE: action; session_id optional when exactly one session is active; address as '0x' hex string; read_bytes ≤65536 per call (split larger reads); Memory scanning has moved to ppsspp_scan.

    BEHAVIOR: READ-ONLY. read_string is ASCII-only (use read_bytes + Shift-JIS decode for game text). Reading code segments: use ppsspp_disassemble — MCP provides no IR-encoding detection (a read_u32 over JIT-IR bytes just returns the raw value).

    RETURNS: {action, address, value, size, text, file} — read_bytes has output=value (default; byte list + hex text) / hex (text only, value=null) / file (paths + 64-byte preview; payload saved under .ppsspp-dfx/output/memory_reads/)."""
    session_id = await resolve_session_id(session_id)
    if action not in _READ_ACTIONS:
        raise ArgsInvalid(f"invalid action={action!r}; expected one of {_READ_ACTIONS}")
    address_int = parse_address(address)
    # read_bytes/read_u32/read_string require a non-zero address; scan uses
    # start_addr/end_addr instead (address is ignored).
    if address_int <= 0:
        raise ArgsInvalid(f"address must be > 0 for action={action!r}")
    # G1 file-mode locals — only populated for read_bytes + output="file"
    file_bin_path = ""
    file_summary = ""
    # set for a read that succeeded but whose value needs a caveat (an
    # unaligned multi-byte read, an ambiguous empty string); surfaced in `text`.
    caveat_note = ""

    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_read_memory", "action": action, "session_id": session_id},
    )

    async with session_client(session_id) as client:
        if action == "read_bytes":
            # `size=True` used to pass as a 1-byte read.
            size = require_int_not_bool(size, "size")
            if size <= 0:
                raise ArgsInvalid("size must be > 0 for read_bytes")
            # Bound single reads — a 1 MB read
            # succeeds but produces a multi-second response whose token
            # cost is unbounded. Chunk via multiple calls instead.
            if size > _MAX_READ_BYTES:
                raise ArgsInvalid(
                    f"size ({size}) exceeds the single-read cap "
                    f"({_MAX_READ_BYTES} bytes); split the request into "
                    f"multiple read_bytes calls"
                )
            raw = await client.read_bytes(address=address_int, size=size)
            result = MemoryReadResult(
                action=action, address=address_int, value=list(raw), size=len(raw)
            )
            if output == "file":
                # G1: keep a large payload off the model context —
                # raw bytes + hex dump go to disk; the response
                # carries paths and a 64-byte preview only.
                stem = f"mem_{address_int:08X}_{len(raw)}"
                file_bin_path = await save_output_bytes("memory_reads", f"{stem}.bin", bytes(raw))
                await save_output_text(
                    "memory_reads",
                    f"{stem}.bin.hex.txt",
                    " ".join(f"{b:02X}" for b in raw),
                )
                preview = " ".join(f"{b:02X}" for b in raw[:64])
                ellipsis = " ..." if len(raw) > 64 else ""
                file_summary = (
                    f"0x{address_int:08X}: saved {len(raw)} bytes to "
                    f"{file_bin_path} (hex dump: {stem}.bin.hex.txt); "
                    f"preview: {preview}{ellipsis}"
                )
        elif action == "read_u32":
            val = await client.read_u32(address=address_int)
            result = MemoryReadResult(action=action, address=address_int, value=val, size=4)
            # An unaligned multi-byte read succeeds on PSP but is a
            # classic pointer bug, so it must not pass unremarked.
            if address_int % 4 != 0:
                caveat_note = (
                    f"unaligned read: 0x{address_int:08X} is not 4-byte "
                    f"aligned; a 4-byte value from this address usually "
                    f"indicates a pointer/offset bug"
                )
        elif action == "read_string":
            # Never call PPSSPP memory.readString —
            # it strnlens to the end of valid memory with no length
            # parameter, and a giant response from a non-string region
            # (e.g. code) kills the WebSocket connection. Always do a
            # bounded read + local NUL scan.
            # Default (max_len<=0) stays 4096 as documented;
            # explicit values are honored up to 65536 (the client's
            # old hard 4096 re-clamp is gone).
            cap = DEFAULT_STRING_CAP if max_len <= 0 else min(max_len, MAX_SINGLE_READ_BYTES)
            text_val = await client.read_string(address=address_int, max_length=cap)
            byte_count = len(text_val.encode("utf-8", errors="replace"))
            # Client and tool caps are both 65536 (the client
            # previously re-clamped to 4096, silently truncating). Signal
            # truncation when the decoded string reached the cap — exact
            # for ASCII, approximate for binary garbage with replacement
            # characters.
            result = MemoryReadResult(
                action=action,
                address=address_int,
                value=text_val,
                size=byte_count,
                truncated=(byte_count >= cap),
            )
            if byte_count == 0:
                # G-16 (FR-016): '' is AMBIGUOUS — a NUL first byte and an
                # address with no live data look identical, because PPSSPP
                # answers unreadable addresses with zeros, not an error (deep
                # test P2-09/P2-10). Name the ambiguity for the caller.
                caveat_note = (
                    f"empty string at 0x{address_int:08X}; the address may be unreadable "
                    "or not yet loaded in this state — verify before reading '' as 'empty'"
                )
    view = MemoryReadResponse.from_result(result)
    if action == "read_bytes" and output == "hex":
        # G1: the hex dump in `text` carries the same payload as the
        # int-array channel at roughly half the characters — drop it.
        view = view.model_copy(update={"value": None})
    elif action == "read_bytes" and output == "file":
        view = view.model_copy(update={"value": None, "text": file_summary, "file": file_bin_path})
    if caveat_note:
        # MemoryReadResponse is a FrozenModel with no `note` field, so the
        # caveat goes into `text` - the channel the caller already reads.
        view = view.model_copy(update={"text": f"{view.text} {caveat_note}".strip()})
    return view.model_dump(mode="json")


@mcp.tool(
    name="ppsspp_write_memory",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
    ),
)
@translate_tool_errors
async def write_memory(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    address: Annotated[
        str,
        Field(
            description=("Target address, as a hex string (e.g. '0x08804000')."),
        ),
    ],
    data: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Value to write, as a string. For format='u8'/'u16'/'u32', "
                "a hex string (e.g. '0x00000001') or decimal string (e.g. "
                "'1'). For format='bytes', a hex string (e.g. 'AABBCCDD') "
                "or base64 string. Optional when 'value' is supplied ("
                "alias); omitting both is an error."
            ),
        ),
    ] = None,
    value: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Alias for 'data'. Accepted because sibling tools take "
                "a 'value'; 'data' remains the canonical name and wins "
                "when both are supplied."
            ),
        ),
    ] = None,
    format: Annotated[  # noqa: A002 — name kept for API stability (MCP field)
        Literal["u8", "u16", "u32", "bytes"],
        Field(
            default="u32",
            description=(
                "Write format. 'u32' (default) writes a 32-bit int. "
                "'u8'/'u16' write byte/halfword granules (byte patches). "
                "'bytes' writes raw bytes (data is hex-decoded)."
            ),
        ),
    ] = "u32",
    force: Annotated[
        bool,
        Field(
            default=False,
            description=(
                "Set to True to write to protected code/data regions of "
                "the modules loaded in THIS session (kernel memory below "
                "0x08800000, plus the top.prx code section as reported by "
                "the live module list); declared data addresses from "
                "addresses.yaml are exempt. Writing to those ranges "
                "without force=True raises ToolError to prevent "
                "accidental crashes."
            ),
        ),
    ] = False,
) -> MemoryWriteOutput:
    """PURPOSE: Write u8/u16/u32 or raw bytes to memory.

    USAGE: session_id + address ('0x' hex) + data + format ('u8'|'u16'|'u32'|'bytes'; bytes accepts hex or base64).

    BEHAVIOR: DESTRUCTIVE. Protected ranges (kernel, top.prx code) need force=true (PROTECTED_ADDRESS).

    RETURNS: {address, format, bytes_written, value, text}."""
    # accept the family-conventional `value` as an alias for `data`.
    # `data` is canonical and wins when both are present. Validated
    # before the session lookup so a missing payload names the missing
    # argument instead of surfacing as SESSION_NOT_FOUND.
    if value is not None and data is None:
        data = value
    if data is None or data == "":
        raise ArgsInvalid(
            "no payload: pass data (canonical) or value (alias), e.g. data='0x00000001'"
        )

    # Check protected code-section ranges.
    # For bytes format, decode data first so we can check the full range
    # [address, address + len(decoded_bytes)) for overlap with protected
    # ranges (not just the start address).
    address_int = parse_address(address)
    write_bytes = (
        len(_decode_bytes_input(data))
        if format == "bytes"
        # Check with the REAL write granularity — byte_count=0 made
        # the guard treat a u32 write as 1 byte, so a u32 at (protected -
        # 3) slipped past the boundary check and corrupted the last bytes
        # into the protected range.
        else {"u8": 1, "u16": 2, "u32": 4}[format]
    )
    # This pre-flight guard must be side-effect free, so it cannot ask
    # the debugger for its module list. It uses the conservative
    # config-derived extent; the runtime-refined pass runs below once the
    # session is open (see service/memory_protection).
    check_protected_address_static(address_int, byte_count=write_bytes, force=force)

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_write_memory",
            "session_id": session_id,
            "address": address_int,
            "format": format,
            "force": force,
        },
    )

    bytes_written = 0
    written_value: int | None = None
    # Pre-decode bytes data if format='bytes' (may already be decoded
    # for the protected-address check above).
    decoded_bytes: bytes | None = None
    if format == "bytes":
        decoded_bytes = _decode_bytes_input(data)
        if not decoded_bytes:
            # An empty payload would pass every check and return
            # "success" having written nothing — fail loudly instead.
            raise ArgsInvalid("data decodes to zero bytes for format='bytes'; nothing to write")
    async with session_client(session_id) as client:
        # The authoritative check. The module list is
        # per session, so this answers "is this address code in the
        # layout THIS session loaded?" — and it is what lets ordinary
        # data variables inside the module image be written without a
        # blanket exemption. When the list is unavailable the helper
        # logs why and returns None, and the check then falls back to
        # the CONSERVATIVE extent; it is never skipped, because the
        # pre-flight above only covers base + safety margin.
        session_modules = await resolve_session_modules(client, session_id)
        check_protected_address(
            address_int,
            byte_count=write_bytes,
            force=force,
            modules=session_modules,
        )
        if format in ("u8", "u16", "u32"):
            value_int = parse_value(data)
            limit = {"u8": 0xFF, "u16": 0xFFFF, "u32": 0xFFFFFFFF}[format]
            if not 0 <= value_int <= limit:
                raise ToolError(
                    f"value {data!r} out of range for format={format!r} (expected 0..{limit:#x})",
                    code="ADDR_INVALID",
                )
            if format == "u8":
                await client.write_u8(address=address_int, value=value_int)
                bytes_written = 1
            elif format == "u16":
                await client.write_u16(address=address_int, value=value_int)
                bytes_written = 2
            else:
                await client.write_u32(address=address_int, value=value_int)
                bytes_written = 4
            written_value = value_int
        else:  # bytes
            # decoded_bytes is guaranteed non-empty here (the
            # pre-check above raises on empty), so no fallback re-decode.
            raw = decoded_bytes
            # 不变式：format='bytes' 时上面已预解码且空值已早拒；此断言仅
            # 向类型检查器传达，不是运行时校验。
            assert raw is not None
            if len(raw) > MAX_WRITE_BYTES:
                # Symmetric with the 64KiB read cap —
                # fail fast with the chunking instruction instead of
                # shipping a multi-MB WS frame.
                raise ArgsInvalid(
                    f"data is {len(raw)} bytes; format='bytes' is capped "
                    f"at {MAX_WRITE_BYTES} — write in chunks"
                )
            await client.write_bytes(address=address_int, data=raw)
            bytes_written = len(raw)

    result = MemoryWriteResult(
        address=address_int,
        format=format,
        bytes_written=bytes_written,
        value=written_value,
    )
    return MemoryWriteResponse.from_result(result).model_dump(mode="json")


@mcp.tool(
    name="ppsspp_disassemble",
    annotations=ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)
@translate_tool_errors
async def disassemble(
    address: Annotated[
        str,
        Field(
            description=("Starting address for disassembly, as a hex string (e.g. '0x08804000')."),
        ),
    ],
    count: Annotated[
        int,
        Field(
            default=10,
            description=(
                "Number of instructions to disassemble. Default 10. "
                f"count=0 is treated as 'use the default' and returns 10 "
                f"instructions (the response carries a note saying so). "
                f"Capped at {_MAX_DISASM_COUNT} to prevent oversized responses; "
                "a larger value is clamped and the note reports the clamp."
            ),
        ),
    ] = 10,
    session_id: Annotated[
        str | None,
        Field(
            description=(
                "Active session ID; omit to auto-resolve when exactly one session is active."
            ),
        ),
    ] = None,
) -> DisassemblyOutput:
    """PURPOSE: Disassemble N MIPS instructions at a given address.

    USAGE: address required; session_id optional when exactly one session is active; count optional (default 10).

    BEHAVIOR: READ-ONLY. Calls memory.disasm via WebSocket. Does not modify memory or CPU state.

    RETURNS: {address, count, instructions: [{address, text}...]}.
    """
    # a negative count is a caller mistake, not a request for zero
    # instructions. It used to return an empty list echoing `count: 0`,
    # indistinguishable from "you asked for 0". Reject BEFORE the session
    # lookup so the caller sees the real cause, not SESSION_NOT_FOUND.
    if count < 0:
        raise ArgsInvalid(
            f"count must be >= 0 (got {count}); pass 0 or omit the argument "
            f"to get the default of 10"
        )
    session_id = await resolve_session_id(session_id)
    address_int = parse_address(address)
    substitution_note: str | None = None
    if count == 0:
        count = 10
        substitution_note = "count=0 is treated as 'use the default'; returned 10 instructions"

    # Cap count to prevent oversized responses.
    effective_count = min(count, _MAX_DISASM_COUNT)
    if effective_count != count:
        substitution_note = (
            f"count {count} exceeded the cap of {_MAX_DISASM_COUNT}; "
            f"returned {_MAX_DISASM_COUNT} instructions"
        )
        count = effective_count

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_disassemble",
            "session_id": session_id,
            "address": address_int,
            "count": count,
        },
    )
    async with session_client(session_id) as client:
        lines = await client.disasm(address=address_int, count=effective_count)
    # Simplify instruction fields to keep only essential ones.
    instructions = [_simplify_disasm_line(line) for line in lines]
    result = DisassemblyResult(
        address=address_int, count=len(instructions), instructions=instructions
    )
    response = DisassemblyResponse.from_result(result).model_dump(mode="json")
    # PPSSPP fills placeholder "-" text for unmapped/invalid addresses
    # instead of erroring. Surface that explicitly — a wall of "-" silently
    # read as "valid empty code" misled a live session.
    if instructions and all(str(ins.get("text", "")).strip() in ("-", "") for ins in instructions):
        response["note"] = (
            "all instructions are placeholders ('-') — the address range "
            "is likely unmapped or unreadable, not empty code"
        )
    elif substitution_note:
        response["note"] = substitution_note
    return response


def _simplify_disasm_line(line: dict[str, Any]) -> dict[str, Any]:
    """Keep only essential fields from a disasm line.

    PPSSPP's memory.disasm may return extra fields (encoding,
    branchDelay, isBranch, isStemmed, etc.) that bloat the response.
    Keep only address/text/name/params. If `text` is missing but
    name/params are present, reconstruct text from them.
    """
    if not isinstance(line, dict):
        return {"text": str(line)}
    out: dict[str, Any] = {}
    for f in _DISASM_KEEP_FIELDS:
        if f in line:
            out[f] = line[f]
    # Reconstruct text if missing (some PPSSPP versions split into
    # name+params without a combined text field).
    if "text" not in out:
        name = out.get("name", "")
        params = out.get("params", "")
        if name and params:
            out["text"] = f"{name} {params}"
        elif name:
            out["text"] = name
    return out


def _decode_bytes_input(data: int | str) -> bytes:
    """Decode the `data` argument for format='bytes'.

    Accepts hex string (e.g. 'AABBCCDD' or '0xAABBCCDD') or base64 string.
    Hex is detected by even-length digits after optional 0x prefix and
    containing only hex chars; otherwise base64 is attempted.
    """
    if isinstance(data, int):
        raise ArgsInvalid("data must be str when format='bytes'")
    s = data.strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    # Try hex first.
    try:
        if len(s) % 2 == 0 and all(c in "0123456789abcdefABCDEF" for c in s):
            return bytes.fromhex(s)
    except ValueError:
        pass
    # Fall back to base64.
    try:
        return base64.b64decode(s, validate=True)
    except Exception as e:
        raise ArgsInvalid(f"could not decode data as hex or base64: {e}") from e
