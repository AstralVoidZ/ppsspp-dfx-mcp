"""The MCP `instructions` string — the always-on usage briefing.

`MCPServer(instructions=...)` is returned once in the `initialize` response and
injected by the client into the model's context for the whole session, so this
text is read *before* any tool is called and stays in context afterwards. That
makes it the only channel for **cross-tool knowledge** — the things no single
tool's `description`/schema can state:

* the order tools must be called in (bring-up, session teardown);
* which tools accept an omitted `session_id` and which do not;
* invariants that span every tool taking an address;
* where to spend context deliberately;
* the first move after an error code.

**What must NOT go here** (the budget is the model's system prompt, and
`eval/` variant B1 measures tool surface + instructions *without* the skill):

* anything already in a tool's own `description` — parameter defaults, return
  fields, per-tool preconditions. Those load with the tool and cannot go stale
  here. Duplicating them is how an instructions block turns into a second,
  divergent copy of the tool surface. (Concretely: the session-lock semantics of
  `ppsspp_breakpoint(action="wait")` are documented on that tool; only the *cross-tool*
  consequence — "the first cpu.stepping broadcast wins" — belongs here.)
* anything a tool response carries (file paths, ids, paginated lists);
* marketing, rationale, implementation detail.

`tests/unit/l2_mcp_contract/test_instructions_contract.py` pins the structure
and the load-bearing facts, so a future edit cannot silently drop a section or
re-introduce a claim that the code contradicts.
"""

from __future__ import annotations

__all__ = ["INSTRUCTIONS"]

INSTRUCTIONS = """\
PPSSPP debug server — drives a local PPSSPP over its WebSocket debugger for PSP
game-localization work.

## Bring-up
ppsspp_health → ppsspp_session(action="start", iso_path=…, wait_ready=true) →
ppsspp_health(session_id=…) → … → ppsspp_session(action="stop").
`wait_ready=true` returns only once the emulated CPU is up; memory and
disassembly calls before that point fail. Stop the session when you are done.

## session_id
`ppsspp_read_memory`, `ppsspp_disassemble`, `ppsspp_step`,
`ppsspp_screenshot`, `ppsspp_diff_memory`, `ppsspp_context` and `ppsspp_scan` may omit
`session_id` when exactly ONE session is active
(0 sessions → an error telling you to start one; 2+ → `SESSION_AMBIGUOUS`
listing the ids). Every other tool requires it. Do not call
`ppsspp_session(action="list")` merely to obtain an id or check state — those five resolve
it themselves, and CPU/game state come from `ppsspp_query` or
`ppsspp_query(action="game_state")`.

## Addresses — one silent trap
A bare digit string with no `0x` prefix is NOT rejected: it parses as DECIMAL.
`"08804000"` therefore means 8804000 = 0x008656A0 — in range, plausible, and
the wrong address, with the call still succeeding. Always keep the `0x`.

## Context budget
Default to the cheap channel: `ppsspp_disassemble` rather than reading code as
data; `output="file"` for reads beyond a few KB; and reuse the `file_path` a
screenshot or texture/CLUT dump already saved rather than capturing twice.

## Breakpoints
Hits require PPSSPP CPUCore=2 (IR Interpreter) — under JIT they never fire, with
no error. `ppsspp_breakpoint(action="wait"/"trace")` wait without
holding the session lock, so reads and observes keep working, but the first
`cpu.stepping` broadcast wins: arm no other breakpoint and submit no
step/pause/resume until the wait returns. `hit=false` on timeout is pollable and
is not a failure.

## Reading errors
Error text starts with `"[CODE] "`. Triage before digging deeper:
  SESSION_NOT_FOUND     → start a session
  SESSION_BUSY          → another call holds the lock; wait, don't retry in a loop
  BOOT_TIMEOUT          → boot wedge: ppsspp_analyze_log, then session(stop) + restart
  CPU_FREEZE_SUSPECTED  → do NOT restart; screenshot the scene, then step(action="resume")
  CAPTURE_EMPTY         → nothing rendered or bound yet; enter a scene and retry
  PROTECTED_ADDRESS     → write_memory / assemble on kernel memory or top.prx
                          code needs force=true
The ppsspp-dfx skill carries the full error-code matrix and the task playbooks.
"""
