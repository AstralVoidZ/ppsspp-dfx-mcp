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
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp.address import parse_address, parse_value
from ppsspp_dfx_mcp.core.primitives import MAX_SINGLE_READ_BYTES
from ppsspp_dfx_mcp.errors import ArgsInvalid, ToolError, to_tool_error
from ppsspp_dfx_mcp.models.memory import (
    DisassemblyResult,
    MemoryReadResult,
    MemoryWriteResult,
)
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.service.memory_protection import check_protected_address
from ppsspp_dfx_mcp.session.client_helper import resolve_session_id, session_client
from ppsspp_dfx_mcp.tools._common import (
    DEFAULT_STRING_CAP,
    save_output_bytes,
    save_output_text,
    translate_tool_errors,
)
from ppsspp_dfx_mcp.views._contract import derive_output_contract
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
# R9: single source of truth in tools/_common (was a duplicated literal)
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


# Former docstring (kept as comment; description is now the TDQS docstring):
# Aggregate memory read tool.
#
# Action → required params:
# read_bytes → address + size + session_id
# read_u32   → address + session_id
# read_string→ address + session_id
# scan       → pattern + start_addr + end_addr + session_id
# (optional: max_results default 100, chunk_size
# default 4096)
@mcp.tool(
    name="ppsspp_read_memory",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
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
                "pattern + start_addr + end_addr). Optional max_results "
                "(default 100)."
            ),
        ),
    ],
    address: Annotated[
        str,
        Field(
            default="0x0",
            description=(
                "Starting address for read_bytes/read_u32/read_string, as a "
                "hex string (e.g. '0x08804000')."
            ),
        ),
    ] = "0x0",
    size: Annotated[
        int,
        Field(
            default=0,
            description="Number of bytes to read (read_bytes only).",
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

    BEHAVIOR: READ-ONLY. read_u32 on JIT-IR code returns IR encoding (IR_ENCODING_DETECTED) — disassemble code instead. read_string is ASCII-only (use read_bytes + Shift-JIS decode for game text).

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

    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_read_memory", "action": action, "session_id": session_id},
    )

    try:
        async with session_client(session_id) as client:
            if action == "read_bytes":
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
                    file_bin_path = await save_output_bytes(
                        "memory_reads", f"{stem}.bin", bytes(raw)
                    )
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
                val = await client.read_string(address=address_int, max_length=cap)
                byte_count = len(val.encode("utf-8", errors="replace"))
                # Client and tool caps are both 65536 (the client
                # previously re-clamped to 4096, silently truncating). Signal
                # truncation when the decoded string reached the cap — exact
                # for ASCII, approximate for binary garbage with replacement
                # characters.
                result = MemoryReadResult(
                    action=action,
                    address=address_int,
                    value=val,
                    size=byte_count,
                    truncated=(byte_count >= cap),
                )
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e
    view = MemoryReadResponse.from_result(result)
    if action == "read_bytes" and output == "hex":
        # G1: the hex dump in `text` carries the same payload as the
        # int-array channel at roughly half the characters — drop it.
        view = view.model_copy(update={"value": None})
    elif action == "read_bytes" and output == "file":
        view = view.model_copy(update={"value": None, "text": file_summary, "file": file_bin_path})
    return view.model_dump(mode="json")


# Former docstring (kept as comment; description is now the TDQS docstring):
# Write to PPSSPP memory.
#
# Writes to protected code-section addresses (kernel memory or
# top.prx code section) are rejected unless ``force=True``. This
# prevents accidental crashes from JIT cache invalidation issues.
#
# Returns:
# MemoryWriteResponse dict: address + format + bytes_written.
#
# Raises:
# ToolError: on session lookup failure, WS failure, invalid data,
# or write to protected address without force=True.
@mcp.tool(
    name="ppsspp_write_memory",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
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
        str,
        Field(
            description=(
                "Value to write, as a string. For format='u8'/'u16'/'u32', "
                "a hex string (e.g. '0x00000001') or decimal string (e.g. "
                "'1'). For format='bytes', a hex string (e.g. 'AABBCCDD') "
                "or base64 string."
            ),
        ),
    ],
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
                "Set to True to write to protected code-section "
                "addresses (kernel memory < 0x08800000 or top.prx code "
                "section 0x08804000-0x08D34000). Writing to these ranges "
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
    # Check protected code-section ranges.
    # For bytes format, decode data first so we can check the full range
    # [address, address + len(decoded_bytes)) for overlap with protected
    # ranges (not just the start address).
    address_int = parse_address(address)
    if format == "bytes":
        decoded = _decode_bytes_input(data)
        check_protected_address(address_int, byte_count=len(decoded), force=force)
    else:
        # Check with the REAL write granularity — byte_count=0 made
        # the guard treat a u32 write as 1 byte, so a u32 at (protected -
        # 3) slipped past the boundary check and corrupted the last bytes
        # into the protected range.
        check_protected_address(
            address_int,
            byte_count={"u8": 1, "u16": 2, "u32": 4}[format],
            force=force,
        )

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
    try:
        async with session_client(session_id) as client:
            if format in ("u8", "u16", "u32"):
                value_int = parse_value(data)
                limit = {"u8": 0xFF, "u16": 0xFFFF, "u32": 0xFFFFFFFF}[format]
                if not 0 <= value_int <= limit:
                    raise ToolError(
                        f"value {data!r} out of range for format={format!r} "
                        f"(expected 0..{limit:#x})",
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
                await client.write_bytes(address=address_int, data=raw)
                bytes_written = len(raw)
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e

    result = MemoryWriteResult(
        address=address_int,
        format=format,
        bytes_written=bytes_written,
        value=written_value,
    )
    return MemoryWriteResponse.from_result(result).model_dump(mode="json")


# Former docstring (kept as comment; description is now the TDQS docstring):
# Disassemble N MIPS instructions at a given address.
#
# `count` is capped at ``_MAX_DISASM_COUNT`` (100) to prevent
# 645KB+ responses. Instruction fields are simplified to keep only
# address/text/name/params (strips encoding, branchDelay, etc.).
#
# `count <= 0` returns an empty result without calling PPSSPP.
# PPSSPP's memory.disasm with count=0 returns "Missing end parameter"
# error; the tool short-circuits this case.
#
# Returns:
# DisassemblyResponse dict: address + count + instructions.
#
# Raises:
# ToolError: on session lookup failure or WS failure.
@mcp.tool(
    name="ppsspp_disassemble",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
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
                "Number of instructions to disassemble. Capped at "
                f"{_MAX_DISASM_COUNT} to prevent oversized responses."
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
    session_id = await resolve_session_id(session_id)
    # M2: count=0 falls back to the documented default (10) — the previous
    # empty-result behavior read as "unmapped memory". Negative counts
    # still short-circuit to an empty result without calling PPSSPP.
    address_int = parse_address(address)
    if count == 0:
        count = 10
    if count < 0:
        logger.info(
            "tool_call",
            extra={
                "tool": "ppsspp_disassemble",
                "session_id": session_id,
                "address": address_int,
                "count": count,
            },
        )
        result = DisassemblyResult(address=address_int, count=0, instructions=[])
        return DisassemblyResponse.from_result(result).model_dump(mode="json")

    # Cap count to prevent oversized responses.
    effective_count = min(count, _MAX_DISASM_COUNT)

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_disassemble",
            "session_id": session_id,
            "address": address_int,
            "count": count,
        },
    )
    try:
        async with session_client(session_id) as client:
            lines = await client.disasm(address=address_int, count=effective_count)
    except ToolError:
        # Business ToolErrors pass through untranslated; the explicit
        # branch keeps that guarantee independent of to_tool_error's
        # implementation.
        raise
    except Exception as e:
        raise to_tool_error(e) from e
    # Simplify instruction fields to keep only essential ones.
    instructions = [_simplify_disasm_line(line) for line in lines]
    result = DisassemblyResult(
        address=address_int, count=len(instructions), instructions=instructions
    )
    response = DisassemblyResponse.from_result(result).model_dump(mode="json")
    # M2: PPSSPP fills placeholder "-" text for unmapped/invalid addresses
    # instead of erroring. Surface that explicitly — a wall of "-" silently
    # read as "valid empty code" misled a live session (blind-test C1).
    if instructions and all(str(ins.get("text", "")).strip() in ("-", "") for ins in instructions):
        response["note"] = (
            "all instructions are placeholders ('-') — the address range "
            "is likely unmapped or unreadable, not empty code"
        )
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


def _decode_hex_pattern(pattern: str) -> bytes:
    """Decode the `pattern` argument for action='scan'.

    Accepts hex string (e.g. 'AABBCCDD' or '0xAABBCCDD'). Strict hex-only:
    no base64 fallback (unlike _decode_bytes_input, scan patterns are
    always hex by spec).

    Raises:
        ToolError: if the pattern is empty, has odd length, or contains
            non-hex characters.
    """
    s = pattern.strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    if not s:
        raise ArgsInvalid("pattern is empty after stripping 0x prefix")
    if len(s) % 2 != 0:
        raise ArgsInvalid(f"pattern must have even length (got {len(s)} chars: {s!r})")
    if not all(c in "0123456789abcdefABCDEF" for c in s):
        raise ArgsInvalid(f"pattern must be valid hex (got non-hex chars in {s!r})")
    return bytes.fromhex(s)
