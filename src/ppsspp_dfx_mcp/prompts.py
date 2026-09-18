"""Prompts (MCP spec reusable prompt templates).

Implements the delivery U-02 / Q-US-4 decision: a
`memory-breakpoint-wizard` prompt that guides the Agent through the
project's breakpoint workflow (IR Interpreter requirement, mem_set
aggregate actions, hit verification). Deliberately self-contained —
it only references registered tool names, no live session state.

Registered via `@mcp.prompt()` decorator (imported by
`server.register_all_tools()`).
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from ppsspp_dfx_mcp.server import mcp

__all__ = ["memory_breakpoint_wizard", "memory_trace_wizard"]


@mcp.prompt(
    name="memory-breakpoint-wizard",
    description=(
        "Guide the Agent through setting up a memory read/write "
        "breakpoint in PPSSPP: verify prerequisites, set the breakpoint "
        "via ppsspp_breakpoint(mem_set), resume execution, and verify "
        "the hit."
    ),
)
def memory_breakpoint_wizard(
    address: Annotated[
        str,
        Field(
            description=("Memory address to watch, as a hex string (e.g. '0x08804000')."),
        ),
    ],
    size: Annotated[
        int,
        Field(
            default=4,
            ge=1,
            le=64,
            description=(
                "Watch width in bytes (1/2/4 map to u8/u16/u32 hardware "
                "widths; larger values cover a region)."
            ),
        ),
    ] = 4,
    purpose: Annotated[
        str,
        Field(
            default="",
            description=(
                "Optional one-line purpose, woven into the generated "
                "workflow (e.g. 'catch writes to the save flag')."
            ),
        ),
    ] = "",
) -> str:
    """Render the memory-breakpoint setup workflow for the given address."""
    purpose_line = f" Purpose: {purpose}." if purpose else ""
    return f"""Set up and verify a PPSSPP memory breakpoint at {address} (width {size} bytes).{purpose_line}

Work through these steps in order, stopping on the first error:

1. PRECONDITIONS — call ppsspp_health, then ppsspp_session(action=get)
   on the active session. Confirm the PPSSPP build runs with the
   IR Interpreter + WebSocket debugger enabled (breakpoints require the
   IR Interpreter; JIT builds will not hit them).

2. CURRENT STATE — call ppsspp_read_memory(action=read_u32,
   session_id=..., address="{address}") and record the value so we can
   compare after the hit.

3. SET BREAKPOINT — call ppsspp_breakpoint(session_id=...,
   action="mem_set", address="{address}", size={size}, read=true,
   write=true). A memory breakpoint breaks on access, not on a value
   change — if a value-change watch is what we need, pair it with
   ppsspp_state_observer(action="register") probes instead.

4. RESUME — if the CPU is paused, call ppsspp_step(action="resume").
   The game runs until something touches [{address}, {address}+{size}).

5. VERIFY — when the breakpoint hits (CPU paused again), call
   ppsspp_query(action="registers", session_id=...) and read the memory
   at {address} again. Report: who accessed it (PC from the register
   snapshot), read or write, and the old vs new value.

6. CLEAN UP — call ppsspp_breakpoint(action='mem_remove',
   address="{address}") unless the watch should stay armed.
"""


@mcp.prompt(
    name="memory-trace-wizard",
    description=(
        "Guide the Agent through tracing 'what code reads/writes this "
        "address' in PPSSPP: prefer the one-call ppsspp_breakpoint(action='trace'), "
        "fall back to the manual ppsspp_breakpoint(set) + ppsspp_breakpoint(action='wait') "
        "protocol, and interpret the hit scene (PC sits AFTER the access "
        "instruction)."
    ),
)
def memory_trace_wizard(
    address: Annotated[
        str,
        Field(
            description=("Memory address to trace, as a hex string (e.g. '0x08A0D000')."),
        ),
    ],
    purpose: Annotated[
        str,
        Field(
            default="",
            description=(
                "Optional one-line purpose, woven into the generated "
                "workflow (e.g. 'find the writer of the dialog pointer')."
            ),
        ),
    ] = "",
) -> str:
    """Render the address-access tracing workflow for the given address."""
    purpose_line = f" Purpose: {purpose}." if purpose else ""
    return f"""Trace what code accesses {address}.{purpose_line}

Work through these steps in order:

1. PRECONDITIONS — ppsspp_health, then ppsspp_session(action=wait_ready)
   on the active session (memory tracing needs the CPU RUNNING; the game
   must have booted past the title load). Breakpoints need PPSSPP
   CPUCore=2 (IR Interpreter).

2. PREFERRED — ONE CALL: ppsspp_breakpoint(action='trace', session_id=...,
   address="{address}", access="read_write", timeout_s=30,
   want_backtrace=true). It arms the breakpoint, waits for the hit,
   captures the scene, removes the breakpoint, and resumes — all with
   cleanup guaranteed. Read the answer from hits[0]: pc (the code that
   touched the address), mem_hits (counter evidence OUR breakpoint
   fired), backtrace (call chain).

3. FALLBACK — MANUAL PROTOCOL (only if the trace tool is unavailable):
   a) ppsspp_breakpoint(action="mem_set", address="{address}");
   b) loop: ppsspp_breakpoint(action='wait')(timeout_s=15) — hit=false is NOT an
      error, keep polling; the wait does NOT hold the session lock;
   c) on hit: ppsspp_query(action="register", name="pc") + ppsspp_query(action="registers");
   d) cleanup: ppsspp_breakpoint(action="mem_remove") then
      ppsspp_step(action="resume").

4. INTERPRET — the CPU stops AFTER the access instruction executes (a
   hit PC of X usually means the access lives at the instruction just
   before X — ppsspp_disassemble(address=PC-4) to see it). On some
   PPSSPP builds the broadcast's reason/related_address fields are
   empty; rely on pc/mem_hits instead.

5. NEVER arm other breakpoints or submit step/pause/resume while a
   trace or wait is in flight — the first cpu.stepping broadcast wins,
   whoever produced it.
"""
