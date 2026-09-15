"""PPSSPP WebSocket event contract table — single source of truth.

Defines the CPU state requirement, trust level, output size, safety
level, PPSSPP-imposed protocol limits, and diagnostic hint for every
WS event invoked by PpssppDebugClient. Tooling layers (DebugClient,
tool decorators, error translator, L4 contract tests) consume this
table to drive automatic with_stepping, output governance, safety
guards, and error classification.

Source-of-truth references (PPSSPP source):
- Core/Debugger/WebSocket/WebSocket.cpp:28-45 (event classification)
- Core/Core.cpp:114 (Core_IsStepping) + Core.cpp:228-244 (Core_DoSingleStep
  / Core_SingleStep increments steppingCounter)
- Core/Debugger/WebSocket/SteppingSubscriber.cpp (step{Into,Over,Out,
  RunUntil,HLE} — confirms REQUIRED_STEPPING checks)
- Core/Debugger/WebSocket/HLESubscriber.cpp:107,243,322,412,444,486,557
  (Core_IsStepping gates for backtrace/func.{add,remove,removeRange,
  rename,scan}/thread.{wake,stop} via ThreadInfoForStatus)
- Core/Debugger/WebSocket/MemorySubscriber.cpp:262 (memory.readString
  strnlen scan to Memory::g_MemorySize — no length parameter)
- Core/Debugger/WebSocket/DisasmSubscriber.cpp:418,443,474 (searchDisasm
  no $ prefix / single-match loop / assemble single instruction)
- Core/Debugger/WebSocket/GPUStatsSubscriber.cpp:98 (depends_on_flip)
- Core/Debugger/WebSocket/GPUBufferSubscriber.cpp:226-232 (requires
  CPU or GPU stepping)
- Core/Debugger/WebSocket/SteppingBroadcaster.cpp:57-72 (cpu.stepping
  broadcast when steppingCounter changes; cpu.resume broadcast on
  CORE_STEPPING → non-STEPPING transition)

CPU state requirement taxonomy:
- REQUIRED_STEPPING  : PPSSPP gates the event with Core_IsStepping();
                       caller MUST ensure CPU is paused before invoking.
- AUTO_STEPPING       : PPSSPP internally LockMemoryAndCPU (pauses via
                       Core_WaitInactive); caller does nothing special.
- NO_STEPPING         : No CPU state precondition. Returned fields may
                       have trust caveats (see `trust`).
- REQUIRED_RUNNING    : Event depends on Display Flip callback; CPU
                       must be running or the response never arrives
                       (ticketed wait times out).
- REQUIRED_STEPPING_OR_GPU_STEPPING : gpu.buffer.* events — PPSSPP
                       requires either CPU stepping or GPU in stepping
                       mode (GPUBufferSubscriber.cpp:226-232).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


# ---------- Enumerations ----------


class CpuStateRequirement(Enum):
    """PPSSPP-side CPU state precondition for a WS event."""

    REQUIRED_STEPPING = "required_stepping"
    """PPSSPP gates the event with Core_IsStepping(); caller must ensure
    CPU is paused (e.g., via with_stepping) before invoking, otherwise
    PPSSPP returns 'CPU currently running (cpu.stepping first)'."""

    AUTO_STEPPING = "auto_stepping"
    """PPSSPP internally calls LockMemoryAndCPU, which pauses via
    Core_WaitInactive(). Caller does nothing — but be aware the CPU
    will be momentarily paused inside the call."""

    NO_STEPPING = "no_stepping"
    """No CPU state precondition. Returned fields may have trust caveats
    (see the `trust` field of the contract)."""

    REQUIRED_RUNNING = "required_running"
    """Event depends on the Display Flip callback (e.g., gpu.stats.get,
    gpu.record.dump). CPU must be running or the response never arrives
    (ticketed wait times out). Caller MUST verify CPU is running before
    invoking, otherwise a timeout (not a clean error) is returned."""

    REQUIRED_STEPPING_OR_GPU_STEPPING = "required_stepping_or_gpu_stepping"
    """gpu.buffer.* events — PPSSPP requires either CPU stepping or GPU
    in stepping mode (GPUBufferSubscriber.cpp:226-232). Caller should
    ensure CPU is stepping (the MCP-layer uses with_stepping)."""


class TrustLevel(Enum):
    """Trustworthiness of returned fields given the CPU state.

    HIGH   — all returned fields are source-guaranteed trustworthy.
    MEDIUM — some fields trustworthy, others not (see diagnostic_hint).
    LOW    — returned fields are NOT trustworthy in the typical
             invocation state (e.g., pc in cpu.getAllRegs when CPU is
             running); caller should re-query under stepping.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class OutputSize(Enum):
    """Typical response payload size category.

    Used by the output-governance decorator (batch 3) to decide
    truncation / pagination strategy.
    """

    SMALL = "small"    # < 1 KB
    MEDIUM = "medium"  # 1–10 KB
    LARGE = "large"    # 10–100 KB (truncate by default)
    HUGE = "huge"      # > 100 KB (paginate or strip fields)


class SafetyLevel(Enum):
    """Destructiveness of the operation."""

    READ_ONLY = "read_only"
    MUTATING = "mutating"        # mutates memory or register state
    DESTRUCTIVE = "destructive"  # mutates code section / crash risk


# ---------- PPSSPP protocol limit identifiers ----------
#
# Stable string identifiers used by `WsEventContract.ppsspp_limits`.
# Tooling layers match on these to apply protocol-specific adaptations
# (e.g., strip `$` prefix before forwarding to searchDisasm).

PPSSPP_LIMIT_NO_LENGTH_PARAM = "no_length_param"
"""memory.readString has no length parameter; strnlen scans to
Memory::g_MemorySize (32 MB). Use read_bytes + manual NUL find."""

PPSSPP_LIMIT_STRNLEN_SCAN = "strnlen_scan"
"""memory.readString uses strnlen — without a NUL terminator the
response can be 32 MB, causing WS transport timeout."""

PPSSPP_LIMIT_NO_DOLLAR_PREFIX = "no_dollar_prefix"
"""memory.searchDisasm match does not accept `$` prefix on register
names (MIPSDebugInterface.cpp:281-290). Use 'ra' not '$ra'."""

PPSSPP_LIMIT_SINGLE_MATCH_LOOP = "single_match_loop"
"""memory.searchDisasm loop search returns only the first match
(DisasmSubscriber.cpp:443 `found=true; break;`)."""

PPSSPP_LIMIT_NO_TEXT_FIELD = "no_text_field"
"""memory.searchDisasm response has no `text` field — only `address`.
MCP layer must call disassemble to fill text."""

PPSSPP_LIMIT_NO_MULTISTMT_SEMICOLON = "no_multistmt_semicolon"
"""memory.assemble does not support `;`-separated multi-instruction
input (DisasmSubscriber.cpp:474 schema is single 'instruction').
armips treats `;` as a comment delimiter."""

PPSSPP_LIMIT_NO_DEREF = "no_deref"
"""cpu.evaluate does not support `*addr` dereference
(CPUCoreSubscriber.cpp:388 expression parser limit)."""

PPSSPP_LIMIT_DEPENDS_ON_FLIP = "depends_on_flip"
"""gpu.stats.get / gpu.record.dump depend on the Display Flip callback
(GPUStatsSubscriber.cpp:98 __DisplayListenFlip). CPU paused → no
frames → response never arrives (ticketed wait times out)."""

PPSSPP_LIMIT_RENDERCOLOR_TITLE_EMPTY = "rendercolor_title_empty"
"""gpu.buffer.renderColor reads the GPU framebuffer; on the title
screen the framebuffer may be uninitialised → empty response."""

PPSSPP_LIMIT_STEPINTO_FIRST_CALL_PAUSE_ONLY = "stepinto_first_call_pause_only"
"""cpu.stepInto (SteppingSubscriber.cpp:92-98): when CPU is NOT stepping,
the first call only pauses (Core_EnableStepping(true, "cpu.stepInto", 0))
and returns WITHOUT stepping. The returned PC is the current PC, not
PC+1. Caller must invoke stepInto again to actually advance."""

PPSSPP_LIMIT_STEPOVER_OUT_BREAKPOINT_MAY_NEVER_HIT = "stepover_out_breakpoint_may_never_hit"
"""cpu.stepOver / cpu.stepOut set a temporary breakpoint and resume
(SteppingSubscriber.cpp:137-227). If the breakpoint address is never
reached (e.g., infinite loop, no stack frame for stepOut), CPU runs
indefinitely → ticketed wait times out. This is expected PPSSPP
behaviour, not an MCP bug."""


# ---------- Contract dataclass ----------


@dataclass(frozen=True)
class WsEventContract:
    """Static contract for a single PPSSPP WS event.

    Fields:
        event: WS event name (e.g., 'cpu.getAllRegs', 'memory.readString').
        cpu_state: PPSSPP-side CPU state precondition.
        trust: Trustworthiness of returned fields given the cpu_state.
        output: Typical response payload size category.
        safety: Destructiveness of the operation.
        ppsspp_limits: Tuple of PPSSPP_LIMIT_* identifiers documenting
            protocol-level limitations the MCP layer must adapt to.
        diagnostic_hint: Human-readable hint shown when the event fails
            (e.g., on timeout, CPU state error, or PPSSPP_LIMIT_* hit).
        fire_and_forget: True if the event is sent without awaiting a
            ticketed response — completion is signalled by a separate
            broadcast (e.g., cpu.stepInto → cpu.stepping broadcast).
            Such events have NO immediate response payload.
        params: Parameter names the MCP client sends for this event
            (R10). A trailing '?' marks an optional parameter. The L1
            param-shape sweep test diffs these declarations against
            what PpssppDebugClient actually puts on the wire.
    """

    event: str
    cpu_state: CpuStateRequirement
    trust: TrustLevel
    output: OutputSize
    safety: SafetyLevel
    ppsspp_limits: tuple[str, ...] = ()
    diagnostic_hint: str = ""
    fire_and_forget: bool = False
    params: tuple[str, ...] = ()


# ---------- Contract table ----------
#
# Every WS event invoked by PpssppDebugClient MUST have an entry here.
# L4 contract tests (batch 7) verify coverage by diffing the events
# referenced in debug_client.py against the keys of WS_EVENT_CONTRACTS.


WS_EVENT_CONTRACTS: dict[str, WsEventContract] = {
    # ── Memory (AUTO_STEPPING — LockMemoryAndCPU handles pause) ───────
    "memory.read_u8": WsEventContract(
        event="memory.read_u8",
        params=("address",),
        cpu_state=CpuStateRequirement.AUTO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="",
    ),
    "memory.read_u16": WsEventContract(
        event="memory.read_u16",
        params=("address",),
        cpu_state=CpuStateRequirement.AUTO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="",
    ),
    "memory.read_u32": WsEventContract(
        event="memory.read_u32",
        params=("address",),
        cpu_state=CpuStateRequirement.AUTO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="read_u32 on the code section returns IR encoding when CPU is in JIT-IR mode; use disassemble instead",
    ),
    "memory.read": WsEventContract(
        event="memory.read",
        params=("address", "size"),
        cpu_state=CpuStateRequirement.AUTO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.LARGE,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="Large reads (>= 64 KB) may approach WS frame limits; chunk if needed",
    ),
    "memory.readString": WsEventContract(
        event="memory.readString",
        cpu_state=CpuStateRequirement.AUTO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.HUGE,
        safety=SafetyLevel.READ_ONLY,
        ppsspp_limits=(
            PPSSPP_LIMIT_NO_LENGTH_PARAM,
            PPSSPP_LIMIT_STRNLEN_SCAN,
        ),
        diagnostic_hint=(
            "memory.readString has no length param; strnlen scans to "
            "Memory::g_MemorySize (32 MB) when no NUL terminator is present, "
            "causing WS transport timeout. Use read_bytes(maxLen) + manual "
            "NUL find instead (batch 2 adaptation)."
        ),
    ),
    "memory.write_u8": WsEventContract(
        event="memory.write_u8",
        params=("address", "value"),
        cpu_state=CpuStateRequirement.AUTO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="",
    ),
    "memory.write_u16": WsEventContract(
        event="memory.write_u16",
        params=("address", "value"),
        cpu_state=CpuStateRequirement.AUTO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="",
    ),
    "memory.write_u32": WsEventContract(
        event="memory.write_u32",
        params=("address", "value"),
        cpu_state=CpuStateRequirement.AUTO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="Writing to the code section may crash PPSSPP; the MCP safety_guard (batch 4) rejects code-section addresses unless force=True",
    ),
    "memory.write": WsEventContract(
        event="memory.write",
        params=("address", "base64"),
        cpu_state=CpuStateRequirement.AUTO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="Large writes to the code section may crash PPSSPP",
    ),

    # ── CPU (NO_STEPPING for reads, REQUIRED_STEPPING for writes) ────
    "cpu.getAllRegs": WsEventContract(
        event="cpu.getAllRegs",
        params=("thread?",),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.LOW,
        output=OutputSize.MEDIUM,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "PC in the GPR category is inaccurate unless CPU is stepping "
            "(CPUCoreSubscriber.cpp:105). Use safe_get_pc() for high-trust PC."
        ),
    ),
    # cpu.getReg is invoked by
    # PpssppDebugClient.get_reg (single-register read, far cheaper than a
    # full getAllRegs dump) — the file invariant above requires an entry.
    "cpu.getReg": WsEventContract(
        event="cpu.getReg",
        params=("name", "thread?"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.LOW,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "Accepts ABI names ('a0'/'v0'/'t9'/...) plus special-cased "
            "'pc'/'hi'/'lo'; rejects numeric-style names with an "
            "'Invalid name parameter' error "
            "(MIPSDebugInterface.cpp:280-290). PC is inaccurate unless "
            "CPU is stepping."
        ),
    ),
    "cpu.setReg": WsEventContract(
        event="cpu.setReg",
        params=("name", "value", "thread?"),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="Call step(pause) first; PPSSPP rejects setReg when CPU is running",
    ),
    "cpu.evaluate": WsEventContract(
        event="cpu.evaluate",
        params=("expression", "thread?"),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        ppsspp_limits=(PPSSPP_LIMIT_NO_DEREF,),
        diagnostic_hint=(
            "Call step(pause) first; PPSSPP rejects evaluate when CPU is "
            "running. Expressions do not support *addr dereference."
        ),
    ),
    "cpu.status": WsEventContract(
        event="cpu.status",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="",
    ),

    # ── Stepping (fire-and-forget — completion signalled by broadcast) ─
    "cpu.stepInto": WsEventContract(
        event="cpu.stepInto",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        ppsspp_limits=(PPSSPP_LIMIT_STEPINTO_FIRST_CALL_PAUSE_ONLY,),
        fire_and_forget=True,
        diagnostic_hint=(
            "Fire-and-forget; completion is signalled by the cpu.stepping "
            "broadcast. NOTE: when CPU is NOT stepping, the first stepInto "
            "only pauses the CPU (does not advance PC) — invoke again to "
            "actually step."
        ),
    ),
    "cpu.stepOver": WsEventContract(
        event="cpu.stepOver",
        params=(),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        ppsspp_limits=(PPSSPP_LIMIT_STEPOVER_OUT_BREAKPOINT_MAY_NEVER_HIT,),
        fire_and_forget=True,
        diagnostic_hint=(
            "Fire-and-forget; requires CPU stepping (SteppingSubscriber.cpp:"
            "140-141). Sets a temporary breakpoint and resumes — if the "
            "breakpoint is never hit, the cpu.stepping broadcast never "
            "arrives and the wait times out (expected PPSSPP behaviour)."
        ),
    ),
    "cpu.stepOut": WsEventContract(
        event="cpu.stepOut",
        params=(),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        ppsspp_limits=(PPSSPP_LIMIT_STEPOVER_OUT_BREAKPOINT_MAY_NEVER_HIT,),
        fire_and_forget=True,
        diagnostic_hint=(
            "Fire-and-forget; requires CPU stepping. Returns 'Could not find "
            "function call to step out into' if the stack walk yields < 2 "
            "frames (e.g., idle thread). Otherwise sets a temporary "
            "breakpoint at the caller and resumes — same timeout caveat "
            "as stepOver."
        ),
    ),
    "cpu.runUntil": WsEventContract(
        event="cpu.runUntil",
        params=("address",),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        fire_and_forget=True,
        diagnostic_hint=(
            "Fire-and-forget; sets a temporary breakpoint at `address` and "
            "resumes. If the address is never reached, the cpu.stepping "
            "broadcast never arrives and the wait times out (expected)."
        ),
    ),
    "cpu.nextHLE": WsEventContract(
        event="cpu.nextHLE",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        fire_and_forget=True,
        diagnostic_hint=(
            "Fire-and-forget; calls hleDebugBreak() and resumes. If no HLE "
            "callback is hit, the cpu.stepping broadcast never arrives and "
            "the wait times out (expected)."
        ),
    ),
    "cpu.stepping": WsEventContract(
        # NOTE: this is the pause primitive (fire_and_forget 'cpu.stepping'
        # event in PPSSPP). Distinct from the 'cpu.stepping' BROADCAST
        # pushed by SteppingBroadcaster. The contract here describes the
        # pause primitive invocation.
        event="cpu.stepping",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        fire_and_forget=True,
        diagnostic_hint="Pause primitive — confirms via cpu.status stepping=True poll",
    ),
    "cpu.resume": WsEventContract(
        event="cpu.resume",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        fire_and_forget=True,
        diagnostic_hint=(
            "Resume primitive (fire-and-forget call) — confirms via "
            "cpu.status stepping=False poll. ALSO emitted as a broadcast "
            "by SteppingBroadcaster.cpp:64 when prevState_ == CORE_STEPPING "
            "&& coreState != CORE_STEPPING && Core_IsActive() (consumed by "
            "GameStateObserver.wait_for_resume for broadcast-confirmed resume)."
        ),
    ),

    # ── Breakpoints (NO_STEPPING — PPSSPP does not gate these) ───────
    "cpu.breakpoint.add": WsEventContract(
        event="cpu.breakpoint.add",
        params=("address", "enabled", "condition?", "log?", "logFormat?"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="",
    ),
    "cpu.breakpoint.remove": WsEventContract(
        event="cpu.breakpoint.remove",
        params=("address",),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="",
    ),
    "cpu.breakpoint.list": WsEventContract(
        event="cpu.breakpoint.list",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.MEDIUM,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="",
    ),
    "cpu.breakpoint.update": WsEventContract(
        event="cpu.breakpoint.update",
        params=("address", "enabled?", "log?", "condition?", "logFormat?"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="PPSSPP matches breakpoints by address; update returns 'not found' if no breakpoint exists at the given address",
    ),
    "memory.breakpoint.add": WsEventContract(
        event="memory.breakpoint.add",
        params=("address", "size", "enabled", "read", "write", "change", "log", "condition?", "logFormat?"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="",
    ),
    "memory.breakpoint.remove": WsEventContract(
        event="memory.breakpoint.remove",
        params=("address", "size"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="",
    ),
    "memory.breakpoint.list": WsEventContract(
        event="memory.breakpoint.list",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.MEDIUM,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="",
    ),
    "memory.breakpoint.update": WsEventContract(
        event="memory.breakpoint.update",
        params=("address", "size", "enabled?", "log?", "condition?", "logFormat?", "read?", "write?", "change?"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="PPSSPP matches mem breakpoints by address+size pair; update may silently drop read/write/change params)",
    ),

    # ── HLE ──────────────────────────────────────────────────────────
    "hle.thread.list": WsEventContract(
        event="hle.thread.list",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.MEDIUM,
        output=OutputSize.MEDIUM,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "isCurrent field is only trustworthy when CPU is stepping "
            "(HLESubscriber.cpp:65-100, based on currentThread global). "
            "Use safe_get_threads() for high-trust thread snapshots."
        ),
    ),
    "hle.thread.wake": WsEventContract(
        event="hle.thread.wake",
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="Call step(pause) first; PPSSPP rejects thread.wake when CPU is running (ThreadInfoForStatus gate)",
    ),
    "hle.thread.stop": WsEventContract(
        event="hle.thread.stop",
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="Call step(pause) first; PPSSPP rejects thread.stop when CPU is running (ThreadInfoForStatus gate)",
    ),
    "hle.backtrace": WsEventContract(
        event="hle.backtrace",
        params=("thread?",),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="Call step(pause) first; PPSSPP rejects backtrace when CPU is running (HLESubscriber.cpp:557-559)",
    ),
    "hle.module.list": WsEventContract(
        event="hle.module.list",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.MEDIUM,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="",
    ),
    "hle.func.list": WsEventContract(
        event="hle.func.list",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.HUGE,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="May return 700+ KB for games with many HLE functions; use top_n (batch 3) to truncate",
    ),
    "hle.func.scan": WsEventContract(
        event="hle.func.scan",
        params=("address", "size", "remove?"),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="Call step(pause) first; PPSSPP rejects func.scan when CPU is running (HLESubscriber.cpp:486-488)",
    ),
    "hle.func.add": WsEventContract(
        event="hle.func.add",
        params=("name?", "address?", "size?"),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="Call step(pause) first; PPSSPP rejects func.add when CPU is running (HLESubscriber.cpp:243-245)",
    ),
    "hle.func.remove": WsEventContract(
        event="hle.func.remove",
        params=("address",),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="Call step(pause) first; PPSSPP rejects func.remove when CPU is running (HLESubscriber.cpp:322-324)",
    ),

    # ── Disasm (NO_STEPPING — but protocol limits apply) ─────────────
    "memory.disasm": WsEventContract(
        event="memory.disasm",
        params=("address", "count", "thread?"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.LARGE,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="count=0 is rejected by PPSSPP ('Missing end parameter'); the MCP layer (batch 4) returns an empty result instead",
    ),
    "memory.assemble": WsEventContract(
        event="memory.assemble",
        params=("address", "code"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.DESTRUCTIVE,
        ppsspp_limits=(PPSSPP_LIMIT_NO_MULTISTMT_SEMICOLON,),
        diagnostic_hint=(
            "Single instruction only — `nop; nop` assembles only the first "
            "(`;` is an armips comment delimiter). The MCP layer (batch 2) "
            "splits on `;` and assembles each instruction separately."
        ),
    ),
    "memory.searchDisasm": WsEventContract(
        event="memory.searchDisasm",
        params=("address", "match", "displaySymbols", "end?", "thread?"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        ppsspp_limits=(
            PPSSPP_LIMIT_NO_DOLLAR_PREFIX,
            PPSSPP_LIMIT_SINGLE_MATCH_LOOP,
            PPSSPP_LIMIT_NO_TEXT_FIELD,
        ),
        diagnostic_hint=(
            "Register names have no `$` prefix (use 'ra' not '$ra'). "
            "Loop search (end<=address) returns only the first match. "
            "Response has no `text` field — MCP layer (batch 2) calls "
            "disassemble to fill text."
        ),
    ),

    # ── Input (NO_STEPPING) ──────────────────────────────────────────
    "input.buttons.press": WsEventContract(
        event="input.buttons.press",
        params=("button", "duration"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="duration is in frames (1 frame = 1/60 s); default=1",
    ),
    "input.buttons.send": WsEventContract(
        event="input.buttons.send",
        params=("buttons",),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="",
    ),
    "input.analog.send": WsEventContract(
        event="input.analog.send",
        params=("x", "y", "stick"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="",
    ),

    # ── System (NO_STEPPING) ─────────────────────────────────────────
    "game.status": WsEventContract(
        event="game.status",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="",
    ),
    "memory.mapping": WsEventContract(
        event="memory.mapping",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.MEDIUM,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="",
    ),
    "memory.base": WsEventContract(
        event="memory.base",
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="Verified in PPSSPP source: DisasmSubscriber.cpp registers memory.base; returns addressHex (16-digit hex host-space base).",
    ),
    "game.reset": WsEventContract(
        event="game.reset",
        params=("break?",),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.DESTRUCTIVE,
        diagnostic_hint="Causes game state loss",
    ),

    # ── GPU Buffer (REQUIRED_STEPPING_OR_GPU_STEPPING) ────────────────
    "gpu.buffer.renderColor": WsEventContract(
        event="gpu.buffer.renderColor",
        params=("type", "alpha", "stackWidth"),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING_OR_GPU_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.LARGE,
        safety=SafetyLevel.READ_ONLY,
        ppsspp_limits=(PPSSPP_LIMIT_RENDERCOLOR_TITLE_EMPTY,),
        diagnostic_hint=(
            "Requires CPU or GPU stepping. On the title screen the "
            "framebuffer may be uninitialised → empty response (batch 6 "
            "falls back to vram)."
        ),
    ),
    "gpu.buffer.renderDepth": WsEventContract(
        event="gpu.buffer.renderDepth",
        params=("type", "alpha", "stackWidth"),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING_OR_GPU_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.LARGE,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="Requires CPU or GPU stepping",
    ),
    "gpu.buffer.renderStencil": WsEventContract(
        event="gpu.buffer.renderStencil",
        params=("type", "alpha", "stackWidth"),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING_OR_GPU_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.LARGE,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="Requires CPU or GPU stepping",
    ),
    "gpu.buffer.texture": WsEventContract(
        event="gpu.buffer.texture",
        params=("level", "type", "alpha", "stackWidth"),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING_OR_GPU_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.LARGE,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="Requires CPU or GPU stepping; captures the currently-bound texture (not by VRAM address)",
    ),
    "gpu.buffer.clut": WsEventContract(
        event="gpu.buffer.clut",
        params=("type", "alpha", "stackWidth"),
        cpu_state=CpuStateRequirement.REQUIRED_STEPPING_OR_GPU_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.MEDIUM,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="Requires CPU or GPU stepping; captures the currently-bound CLUT",
    ),

    # ── GPU Stats / Record (REQUIRED_RUNNING — depends on Display Flip) ─
    # NOTE on gpu.stats.get dual semantics:
    #   1. Ticketed call (contract below) — caller awaits the next flip's
    #      stats response (PPSSPP GPUStatsSubscriber.cpp:152 Get()).
    #   2. Broadcast (pushed by gpu.stats.feed) — when feed is enabled,
    #      GPUStatsSubscriber.cpp:186-201 Broadcast() pushes
    #      DebuggerGPUStatsEvent each frame. The `ticket` field is only
    #      written when lastTicket_ is non-empty (line 42-43); after the
    #      first feed push, lastTicket_.clear() (line 199), so subsequent
    #      feed-pushed broadcasts carry no ticket and reach the events
    #      queue via _recv_loop fallback (transport.py:178-180). The
    #      broadcast form still depends on Display Flip → REQUIRED_RUNNING
    #      applies to both. See game-state-broadcast spec for the
    #      GameStateObserver gpu.stats.get queue consumer.
    "gpu.stats.get": WsEventContract(
        event="gpu.stats.get",
        params=(),
        cpu_state=CpuStateRequirement.REQUIRED_RUNNING,
        trust=TrustLevel.HIGH,
        output=OutputSize.MEDIUM,
        safety=SafetyLevel.READ_ONLY,
        ppsspp_limits=(PPSSPP_LIMIT_DEPENDS_ON_FLIP,),
        diagnostic_hint=(
            "Requires CPU running (not stepping). PPSSPP pushes stats on "
            "the next GPU flip; if CPU is paused, no frames are rendered "
            "and the ticketed wait times out. The MCP layer (batch 1) "
            "verifies CPU state first and raises CpuStateError instead of "
            "waiting for timeout."
        ),
    ),
    "gpu.record.dump": WsEventContract(
        event="gpu.record.dump",
        params=(),
        cpu_state=CpuStateRequirement.REQUIRED_RUNNING,
        trust=TrustLevel.HIGH,
        output=OutputSize.HUGE,
        safety=SafetyLevel.READ_ONLY,
        ppsspp_limits=(PPSSPP_LIMIT_DEPENDS_ON_FLIP,),
        diagnostic_hint=(
            "Requires CPU running. PPSSPP records the next frame's GE "
            "commands; if CPU is paused, no frames are rendered and the "
            "ticketed wait times out. The MCP layer (batch 1) verifies "
            "CPU state first. The raw response may contain a 300+ KB "
            "base64 payload — batch 3 strips it from the JSON response."
        ),
    ),

    # ── Memory Info (NO_STEPPING) ─────────────────────────────────────
    "memory.info.search": WsEventContract(
        event="memory.info.search",
        params=("match", "address?", "end?", "type?"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "`match` is a case-insensitive substring matched against memory "
            "region TAGS (not type names). Returns a single `extent` (null "
            "if no match), not a list."
        ),
    ),

    # ── Replay (NO_STEPPING) ─────────────────────────────────────────
    # All replay.* events are
    # usable while the CPU is RUNNING — recording requires the CPU to
    # be running so captured button timings are real. See
    # ReplaySubscriber.cpp:26-37 for the protocol surface and
    # docs/experiment/experiment_ppsspp_replay_spike_v1.md for spike
    # evidence.
    "replay.begin": WsEventContract(
        event="replay.begin",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="Begin/resume recording; no params, no extra response data.",
    ),
    "replay.abort": WsEventContract(
        event="replay.abort",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="Abort any replay execution or recording; discards in-progress recording.",
    ),
    "replay.flush": WsEventContract(
        event="replay.flush",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.MEDIUM,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "Flush recorded data; returns {version, base64}. Fails with "
            "'Game not running' if PSP not inited. size must be computed "
            "client-side from len(base64) decoded."
        ),
    ),
    "replay.execute": WsEventContract(
        event="replay.execute",
        params=("version", "base64"),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint=(
            "Execute a replay; params {version, base64}. Does not auto-end "
            "— poll replay.status.executing until False (see wait_complete)."
        ),
    ),
    "replay.status": WsEventContract(
        event="replay.status",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="Returns {executing: bool, saving: bool}.",
    ),
    "replay.time.get": WsEventContract(
        event="replay.time.get",
        params=(),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "Returns {value: uint} — base RTC (power-on time). Constant "
            "during a session. Fails if PSP not inited."
        ),
    ),
    "replay.time.set": WsEventContract(
        event="replay.time.set",
        params=("value",),
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.MUTATING,
        diagnostic_hint="Overwrite base RTC; param {value: uint}.",
    ),

    # ── Game lifecycle broadcasts (GameBroadcaster.cpp, ticketless) ───
    # Pushed when GlobalUIState transitions. Consumed by GameStateObserver
    # to drive the loading→running→paused→running / running→quit state
    # machine. Broadcast carries a `game` field (null or {id, version,
    # title}); no ticket, no response payload — arrives via the events
    # queue.
    "game.start": WsEventContract(
        event="game.start",
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "Game lifecycle broadcast; emitted when game starts "
            "(GameBroadcaster.cpp:86-88 on UISTATE_INGAME && PSP_IsInited()). "
            "Drives GameStateObserver loading→running transition."
        ),
    ),
    "game.pause": WsEventContract(
        event="game.pause",
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "Emitted when game is paused by user (GameBroadcaster.cpp:80-82 "
            "on UISTATE_PAUSEMENU). Distinct from cpu.stepping — this is "
            "the pause menu, not a breakpoint. Drives running→paused "
            "transition; freeze detection is suppressed while paused."
        ),
    ),
    "game.resume": WsEventContract(
        event="game.resume",
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "Emitted when game is resumed (GameBroadcaster.cpp:83-85 on "
            "UISTATE_INGAME && prevState==UISTATE_PAUSEMENU). Drives "
            "paused→running transition."
        ),
    ),
    "game.quit": WsEventContract(
        event="game.quit",
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "Emitted when game quits (GameBroadcaster.cpp:89-91 on "
            "UISTATE_MENU && !PSP_IsInited()). Drives running→quit "
            "transition; freeze detection should recommend session reset."
        ),
    ),

    # ── Log broadcast (LogBroadcaster.cpp, ticketless) ───────────────
    # Pushed for each PPSSPP log message. Default enabled (disallowed.logger
    # defaults false). GameStateObserver defensively calls
    # broadcast.config.set(disallowed={"logger": False}) on start.
    "log": WsEventContract(
        event="log",
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.MEDIUM,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "Log broadcast (LogBroadcaster.cpp:88-103 DebuggerLogEvent). "
            "Fields: event / timestamp / header / message / level (NUMERIC "
            "1-6: 1=NOTICE, 2=ERROR, 3=WARN, 4=INFO, 5=DEBUG, 6=VERBOSE) / "
            "channel. NOTE: level is a number, NOT a string. Default "
            "enabled (disallowed.logger defaults false). GameStateObserver "
            "maps level→Python logging and injects via "
            "logging.getLogger('ppsspp_dfx_mcp.ppsspp_log')."
        ),
    ),

    # ── GPU stats feed (GPUStatsSubscriber.cpp:171, ticketed call) ────
    # Starts/stops the per-frame gpu.stats.get broadcast feed. No
    # immediate response payload beyond the ticket ack. enable defaults
    # true in PPSSPP (req.ParamBool OPTIONAL).
    "gpu.stats.feed": WsEventContract(
        event="gpu.stats.feed",
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "Ticketed call (GPUStatsSubscriber.cpp:171 Feed()). Param "
            "`enable: bool` (optional, defaults true). No immediate "
            "response payload beyond ticket ack. When enabled, PPSSPP "
            "pushes gpu.stats.get broadcasts each frame (see gpu.stats.get "
            "dual-semantics note above). GameStateObserver uses this for "
            "frame-freeze detection (3s silence + game running → "
            "CpuFreezeSuspected)."
        ),
    ),

    # ── Broadcast config (ClientConfigSubscriber.cpp, ticketed calls) ─
    # Per-client disallowed flags. Field names: logger / game / stepping /
    # input. Semantics: true=disabled, false/absent=enabled. Note the
    # field is `logger` (NOT `log`) — disallowed.logger=False means
    # "logger not disabled" i.e. log broadcasts ARE enabled.
    "broadcast.config.get": WsEventContract(
        event="broadcast.config.get",
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "Ticketed call (ClientConfigSubscriber.cpp:40). No params. "
            "Response: {disallowed: {logger?, game?, stepping?, input?}} "
            "— only true flags are serialized. Used to inspect current "
            "per-client broadcast subscription state."
        ),
    ),
    "broadcast.config.set": WsEventContract(
        event="broadcast.config.set",
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint=(
            "Ticketed call (ClientConfigSubscriber.cpp:69). Param "
            "`disallowed: dict` with optional boolean keys logger / game / "
            "stepping / input (true=disable, false/absent=enable). "
            "Response mirrors broadcast.config.get. GameStateObserver "
            "calls disallowed={logger: False} defensively on start "
            "(log broadcast is enabled by default; this is an explicit "
            "confirmation). NOTE: field name is `logger` not `log`."
        ),
    ),

    # ── Version handshake ────────────────────────────────────────────
    "version": WsEventContract(
        event="version",
        cpu_state=CpuStateRequirement.NO_STEPPING,
        trust=TrustLevel.HIGH,
        output=OutputSize.SMALL,
        safety=SafetyLevel.READ_ONLY,
        diagnostic_hint="First message after WS connect; may be ticketed or broadcast depending on PPSSPP version",
    ),
}


# ---------- Lookup helpers ----------


def get_contract(event: str) -> WsEventContract:
    """Return the contract for `event`.

    Raises:
        KeyError: if `event` is not in WS_EVENT_CONTRACTS. This is a
            programming error — every WS event invoked by
            PpssppDebugClient MUST have a contract entry.
    """
    return WS_EVENT_CONTRACTS[event]


def requires_stepping(event: str) -> bool:
    """True if the event's cpu_state is REQUIRED_STEPPING.

    Raises:
        KeyError: if `event` is not in WS_EVENT_CONTRACTS.
    """
    return WS_EVENT_CONTRACTS[event].cpu_state is CpuStateRequirement.REQUIRED_STEPPING


def requires_running(event: str) -> bool:
    """True if the event's cpu_state is REQUIRED_RUNNING.

    Raises:
        KeyError: if `event` is not in WS_EVENT_CONTRACTS.
    """
    return WS_EVENT_CONTRACTS[event].cpu_state is CpuStateRequirement.REQUIRED_RUNNING


def requires_stepping_or_gpu_stepping(event: str) -> bool:
    """True if the event's cpu_state is REQUIRED_STEPPING_OR_GPU_STEPPING.

    Raises:
        KeyError: if `event` is not in WS_EVENT_CONTRACTS.
    """
    return (
        WS_EVENT_CONTRACTS[event].cpu_state
        is CpuStateRequirement.REQUIRED_STEPPING_OR_GPU_STEPPING
    )
