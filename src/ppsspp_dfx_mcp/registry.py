"""ppsspp-dfx-mcp 组合根：服务器实例与脚本暴露链（specs/010 US4 T049）。

本模块自 server.py 整体迁出：`mcp` 实例、`_lifespan` 与脚本暴露链在原文件内
互相循环引用（靠晚绑定成立），必须同处一个模块。server.py 改为从这里导入，
以保持 `ppsspp_dfx_mcp.server.mcp` 的向后兼容。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp import __version__
from ppsspp_dfx_mcp.config import (
    project_root as _config_project_root,
)
from ppsspp_dfx_mcp.instructions import INSTRUCTIONS
from ppsspp_dfx_mcp.middleware import (
    rate_limit_middleware,
    request_id_middleware,
    tool_error_middleware,
)
from ppsspp_dfx_mcp.session import session_manager
from ppsspp_dfx_mcp.spec.script_envelope import envelope_contract

log = logging.getLogger("ppsspp_dfx_mcp")


@asynccontextmanager
async def _lifespan(_app: MCPServer) -> AsyncIterator[None]:
    """Startup: spawn idle GC + load manifest + register exposed scripts.

    Shutdown: cancel GC task, then stop every active session with a
    per-session 5s timeout so PPSSPP processes don't leak when the MCP
    server exits (Ctrl+C / SIGTERM). Manifest and exposed-tool
    registration are best-effort: a malformed manifest logs a warning
    but does NOT abort startup (the 3 script tools remain callable;
    `ppsspp_list_scripts` will return an empty list until
    `ppsspp_reload_scripts` succeeds).
    """
    gc_task = asyncio.create_task(session_manager.idle_gc_loop())
    log.info(
        "lifespan startup: idle GC started (interval=%ds, threshold=%ds)",
        session_manager.IDLE_GC_INTERVAL_S,
        session_manager.IDLE_GC_THRESHOLD_S,
    )
    # Load the manifest and register exposed scripts as dynamic tools.
    # Snapshot the static count BEFORE dynamic
    # script registration — module-file count != tool count, so counting
    # module files understated the static surface. The authoritative
    # count lives in tool_surface_baseline.json, never in prose.
    static_tools = registered_tool_count()
    # Belt-and-suspenders: `_load_manifest_and_register_exposed` only
    # catches `ManifestError`, so any OTHER manifest failure (e.g. a
    # non-UTF8 scripts.yaml -> UnicodeDecodeError) would otherwise escape
    # and abort server startup, taking all static tools down with it.
    # `except Exception` (not BaseException) keeps CancelledError et al.
    # propagating.
    try:
        await _load_manifest_and_register_exposed()
    except Exception as e:  # noqa: BLE001 — best-effort startup; see docstring
        log.warning(
            "lifespan: manifest load/registration failed (%s: %s). "
            "Dynamic script tools are disabled; the %d static tools are "
            "unaffected. Fix or remove the script manifest (scripts.yaml) "
            "and restart to re-enable them.",
            type(e).__name__,
            e,
            static_tools,
        )
    log.info(
        "lifespan: registered %d tools (static=%d + dynamic=%d)",
        registered_tool_count(),
        static_tools,
        registered_tool_count() - static_tools,
    )
    yield
    log.info("lifespan shutdown: cancelling idle GC")
    gc_task.cancel()
    with suppress(asyncio.CancelledError):
        await gc_task
    # Stop every active session so PPSSPP processes don't leak. Each
    # stop_session is sync (kills subprocess + persists), so we offload
    # it to a thread and enforce a 5s timeout — a hung PPSSPP process
    # shouldn't block MCP server exit indefinitely.
    await _shutdown_sessions()


async def _shutdown_sessions() -> None:
    """Stop all active sessions with per-session 5s timeout + logging.

    Uses `gc_idle_sessions()` to reap stale/expired entries first (async,
    offloads blocking ops to threads), then stops each surviving session
    via `stop_session` (also async + offloaded). Per-session
    `asyncio.wait_for(..., timeout=5.0)` ensures a stuck stop call
    doesn't block server exit; on timeout we log a warning and move on.
    """
    # GC stale/expired sessions first (async, non-blocking).
    try:
        gc_ids = await session_manager.gc_idle_sessions()
        if gc_ids:
            log.info(
                "lifespan shutdown: GC'd %d expired session(s): %s",
                len(gc_ids),
                gc_ids,
            )
    except Exception as e:
        log.warning("lifespan shutdown: GC failed: %s", e)

    # List surviving sessions (pure read, no GC side effect).
    try:
        sessions = await session_manager.list_sessions()
    except Exception as e:
        log.warning("lifespan shutdown: failed to list sessions: %s", e)
        return

    if not sessions:
        log.info("lifespan shutdown: no active sessions to stop")
        return

    log.info("lifespan shutdown: stopping %d active session(s)", len(sessions))
    stopped_ok: list[str] = []
    timed_out: list[str] = []
    failed: list[tuple[str, str]] = []
    for sess in sessions:
        sid = sess.session_id
        try:
            await asyncio.wait_for(
                session_manager.stop_session(sid),
                timeout=5.0,
            )
            stopped_ok.append(sid)
            log.info("lifespan shutdown: session %s stopped", sid)
        except TimeoutError:
            timed_out.append(sid)
            log.warning("lifespan shutdown: session %s stop timed out after 5s", sid)
        except Exception as e:
            failed.append((sid, repr(e)))
            log.warning("lifespan shutdown: session %s stop failed: %r", sid, e)
    log.info(
        "lifespan shutdown: session cleanup summary (stopped=%d, timed_out=%d, failed=%d)",
        len(stopped_ok),
        len(timed_out),
        len(failed),
    )


def _build_exposed_wrapper(entry: Any, input_cls: Any, output_cls: Any) -> Callable[..., Any]:
    """Build an async wrapper that delegates to `tools.script.run_script`.

    The wrapper's `__signature__` is derived from the Input model's
    Pydantic fields so the SDK generates a useful `inputSchema` for Agent
    discovery (with field descriptions and types). `entry` is captured
    via the factory's closure scope (NOT exposed as a parameter) so the
    wrapper's signature contains only Input-model fields.

    Args:
        entry: ScriptEntry (with `.name`, `.description`, etc.).
        input_cls: Pydantic BaseModel subclass for the script's input.
        output_cls: Pydantic BaseModel subclass nested under the return
            contract's `output` field (see `spec.script_envelope`).

    Returns:
        Async callable with `__signature__` and `__annotations__` set.
    """
    # Build a list of inspect.Parameter objects, one per Input model field.
    # The SDK's `func_metadata` reads `inspect.signature(fn, eval_str=True)`
    # to generate the inputSchema; setting `__signature__` overrides the
    # default `**kwargs`-only signature.
    params: list[inspect.Parameter] = []
    for field_name, field_info in input_cls.model_fields.items():
        annotation = field_info.annotation if field_info.annotation is not None else Any
        description = field_info.description
        if description:
            # Embed the description via Annotated[...] so it surfaces in the
            # generated JSON schema's `description` field.
            annotated_type: Any = Annotated[annotation, Field(description=description)]
        else:
            annotated_type = annotation
        if field_info.is_required():
            default: Any = inspect.Parameter.empty
        else:
            # default_factory fields report
            # is_required()=False but default=PydanticUndefined — the
            # sentinel must not leak into the signature the SDK
            # introspects to build inputSchema.
            default = field_info.get_default(call_default_factory=True)
            if default is inspect.Parameter.empty or str(type(default)).endswith(
                "PydanticUndefined"
            ):
                default = inspect.Parameter.empty
        params.append(
            inspect.Parameter(
                field_name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotated_type,
            )
        )

    # Capture `entry` via the factory closure (NOT as a default arg, which
    # would expose `_entry` to the SDK and trigger InvalidSignature due to
    # the leading-underscore param name).
    async def _exposed_wrapper(**kwargs: Any) -> dict[str, Any]:
        from ppsspp_dfx_mcp.tools.script import run_script

        # Forward session_id to run_script so the exposed
        # path populates ctx.session_id. When the Input model declares a
        # session_id field it stays in `input` as well (single source —
        # run_script resolves tool-param > Input field to the same value).
        return await run_script(
            name=entry.name,
            input=kwargs or {},
            session_id=kwargs.get("session_id"),
        )

    # Override the wrapper's signature so `inspect.signature(fn)`
    # returns our Pydantic-derived parameters instead of `**kwargs`.
    # Measured 2026-09-08: the overrides above used to DROP the return
    # annotation — and the SDK only emits structuredContent when it can
    # infer an output schema from `inspect.signature(fn, eval_str=True)`.
    # With no return annotation the wrapper's tools answered in the text
    # channel only (verified: func_metadata(wrapper).output_schema was
    # None while decorator-registered tools got one). Keep the return
    # annotation in BOTH override channels.
    #
    # tool-schema-contract (2026-09-13): the annotation is a real contract
    # instead of a bare `dict[str, Any]`. It describes what the wrapper
    # ACTUALLY returns — `run_script()`'s envelope `{name, output,
    # output_model}` — NOT the script's Output model alone: the SDK validates
    # every returned dict against it (`func_metadata.convert_result` →
    # `validate_python`), so nesting `output_cls` under `output` keeps the
    # script's field-level schema visible to the Agent AND matches the real
    # return shape. The envelope shape itself has ONE definition now
    # (`spec.script_envelope`), shared with `run_script`'s returned dict.
    output_contract = envelope_contract(entry.name, output_cls)
    _exposed_wrapper.__signature__ = inspect.Signature(params, return_annotation=output_contract)  # type: ignore[attr-defined]
    # Also set __annotations__ for any caller that introspects annotations
    # directly (belt-and-suspenders; the SDK uses inspect.signature).
    _exposed_wrapper.__annotations__ = {p.name: p.annotation for p in params}  # type: ignore[attr-defined]
    _exposed_wrapper.__annotations__["return"] = output_contract  # type: ignore[attr-defined]
    _exposed_wrapper.__doc__ = (  # type: ignore[attr-defined]
        f"Exposed diagnostic script: {entry.description} "
        f"(category={entry.category}, requires_ppsspp={entry.requires_ppsspp}). "
        f"Input model: {entry.input_model}. Output model: {entry.output_model}."
    )
    return _exposed_wrapper


async def _load_manifest_and_register_exposed() -> None:
    """Load the script manifest at startup and register exposed scripts.

    Best-effort: a missing or malformed manifest logs a warning and skips
    exposed-tool registration. The 3 base script tools
    (list_scripts/run_script/reload_scripts) remain registered regardless.

    This is now the startup call of `sync_exposed_tools()`
    (which is also invoked by `ppsspp_reload_scripts`), so manifest edits
    no longer require a server restart to reach the dynamic tool registry.
    """
    from ppsspp_dfx_mcp.errors import ManifestError
    from ppsspp_dfx_mcp.spec.script_manifest import get_manifest

    manifest = get_manifest()
    try:
        manifest.ensure_loaded()
    except ManifestError as e:
        log.warning("lifespan: manifest load failed; exposed scripts skipped: %s", e)
        return

    report = await sync_exposed_tools()
    log.info(
        "lifespan: registered %d/%d exposed script tools "
        "(added=%d, removed=%d, skipped_skeleton=%d, failed=%d)",
        report["registered"],
        report["declared"],
        len(report["added"]),
        len(report["removed"]),
        len(report["skipped_skeleton"]),
        len(report["failed"]),
    )


# ── Exposed-tool registry ────────────────────────────────────────────────
#
# Tracks which exposed scripts are ACTUALLY registered as dynamic tools so
# that (a) `ppsspp_list_scripts` can surface declared-vs-registered drift
# and (b) `ppsspp_reload_scripts` can add/remove tools to match the
# manifest without a server restart. Key = script name, value = tool name.
# The SDK's `add_tool` is idempotent for the same name; removal goes
# through the same private registry used by
# `registered_tool_names()` below.

_exposed_registry_lock = threading.RLock()

_exposed_registry: dict[str, str] = {}


def registered_exposed_names() -> set[str]:
    """Script names currently registered as `ppsspp_script_*` tools."""
    with _exposed_registry_lock:
        return set(_exposed_registry.keys())


async def _register_exposed_entry(entry: Any, project_root: Any) -> bool:
    """Pre-flight + register one exposed script as a dynamic tool.

    Returns True when the tool was registered (or already present),
    False when skipped/failed. Skeleton-status entries are rejected
    before any import is attempted — unusable capabilities must
    not surface in tools/list.
    """
    if entry.status == "skeleton":
        log.warning(
            "exposed script %r skipped: status=skeleton (run() returns "
            "not_implemented); re-expose it after the body is re-wired",
            entry.name,
        )
        return False

    tool_name = f"ppsspp_script_{entry.name}"
    with _exposed_registry_lock:
        if entry.name in _exposed_registry:
            return True  # already registered; add_tool is idempotent anyway

    from ppsspp_dfx_mcp.tools.script import validate_script_contract

    # Pre-flight: verify the script module imports cleanly AND its
    # contract (Input/Output Pydantic models + async entry fn) is
    # well-formed so we don't register a tool that will explode on
    # first call. Skip + log on failure (the script is still callable
    # via ppsspp_run_script, which surfaces a cleaner error). Uses
    # the shared `validate_script_contract` helper so lifespan and
    # run_script share the same contract validation logic (P1-11).
    # The import executes the script's top-level code — same
    # event-loop-freeze hazard run_script guards against with to_thread.
    try:
        _module, input_cls, output_cls, _fn = await asyncio.to_thread(
            validate_script_contract, entry, project_root
        )
    except Exception as e:
        log.warning(
            "skipping exposed script %r (contract/import error): %s",
            entry.name,
            e,
        )
        return False

    # Build the wrapper via a factory so each wrapper captures its own
    # `entry` (avoids the classic closure-in-loop late-binding bug).
    # The factory sets `__signature__` from the Input model's fields so
    # the SDK generates a non-empty inputSchema with field descriptions.
    _exposed_wrapper = _build_exposed_wrapper(entry, input_cls, output_cls)

    try:
        # Check + add_tool + registry write share ONE critical section:
        # an await happened above (contract validation), so a concurrent
        # reload/lifespan sync may have registered this name meanwhile —
        # re-checking outside the lock would be a check-then-act race
        # producing spurious "failed" entries in the reload report.
        # add_tool is synchronous, so holding the threading lock here is
        # safe (no await inside the critical section).
        with _exposed_registry_lock:
            if entry.name in _exposed_registry:
                return True  # concurrent registration won the race
            mcp.add_tool(
                _exposed_wrapper,
                name=tool_name,
                description=(
                    f"Diagnostic script '{entry.name}': {entry.description} "
                    f"(category={entry.category})."
                ),
                annotations=_DEFAULT_SCRIPT_ANNOTATIONS,
            )
            _exposed_registry[entry.name] = tool_name
    except Exception as e:
        log.warning("failed to register exposed tool %r: %s", tool_name, e)
        return False
    return True


def clear_exposed_registry() -> None:
    """清空脚本暴露注册表（语义化回收 API）。

    _unregister_exposed_tool 按单个脚本注销；本函数用于整体清场
    （如清单全量重载失败后的复位）。持锁执行，避免与注册/注销并发交错。
    """
    with _exposed_registry_lock:
        _exposed_registry.clear()


def _unregister_exposed_tool(script_name: str) -> bool:
    """Remove a dynamic `ppsspp_script_*` tool from the SDK registry.

    Returns True only when something was ACTUALLY removed — i.e. the
    script was in our bookkeeping AND its tool was still present in the
    SDK registry. Returns False when there was nothing to remove (unknown
    script, or the tool was already gone from the SDK), so callers can
    distinguish a real removal from a no-op. Uses the same private SDK
    registry as `registered_tool_names()`.
    """
    with _exposed_registry_lock:
        tool_name = _exposed_registry.pop(script_name, None)
        if tool_name is None:
            return False
        # SDK pop belongs in the same critical section as the registry
        # pop: split phases would let a concurrent re-register slip its
        # add_tool between the two pops and get silently removed.
        removed = mcp._tool_manager._tools.pop(tool_name, None) is not None  # noqa: SLF001 — private SDK API, isolated here
    if not removed:
        log.warning(
            "exposed tool %r was not present in the SDK registry (already removed?)",
            tool_name,
        )
    return removed


async def sync_exposed_tools() -> dict[str, Any]:
    # Async so reload/lifespan callers don't run untrusted module
    # imports on the event loop thread (see _register_exposed_entry).
    """Reconcile the dynamic tool registry with the manifest.

    Desired set = manifest entries with exposed=true AND status=migrated
    (skeleton entries are never registered). Registers missing tools,
    unregisters stale ones, and returns a report dict:

        {declared, registered, added, removed, skipped_skeleton, failed,
         restart_required}

    `restart_required` is computed from the reconciliation result, not a
    constant: it is True only when the SDK's runtime registry could not be
    made to match our bookkeeping (an `add_tool` silently no-opped, or a
    removed tool still lingers in the registry — an SDK limitation only a
    server restart can rebuild). Contract/import failures are reported in
    `failed` and do NOT set it: a reload retries those and a restart would
    not help.
    """

    from ppsspp_dfx_mcp.spec.script_manifest import get_manifest

    manifest = get_manifest()
    manifest.ensure_loaded()
    exposed = manifest.list_exposed()
    project_root = _config_project_root()

    desired: dict[str, Any] = {}
    skipped_skeleton: list[str] = []
    for entry in exposed:
        if entry.status == "skeleton":
            skipped_skeleton.append(entry.name)
        else:
            desired[entry.name] = entry

    with _exposed_registry_lock:
        current = set(_exposed_registry.keys())

    added: list[str] = []
    removed: list[str] = []
    failed: list[str] = []

    for name, entry in desired.items():
        if name in current:
            continue
        if await _register_exposed_entry(entry, project_root):
            if name in registered_exposed_names():
                added.append(name)
        else:
            # Skeleton skip is a policy decision, not a failure; entries
            # that fail contract/import land in `failed`.
            if entry.status != "skeleton":
                failed.append(name)

    for name in sorted(current - set(desired.keys())):
        _unregister_exposed_tool(name)
        removed.append(name)

    with _exposed_registry_lock:
        registered = len(_exposed_registry)
        bookkept_tool_names = set(_exposed_registry.values())

    # Verify the runtime registry really matches our bookkeeping: `add_tool`
    # can silently no-op and the private `_tools.pop` removal can silently
    # miss (SDK limitation). Either leaves the runtime registry diverged
    # from the manifest — a state only a restart can rebuild — so report it
    # instead of returning a hard-coded False.
    runtime_tool_names = registered_tool_names()
    removed_tool_names = {f"ppsspp_script_{name}" for name in removed}
    reconciled = bookkept_tool_names.issubset(runtime_tool_names) and not (
        removed_tool_names & runtime_tool_names
    )
    return {
        "declared": len(exposed),
        "registered": registered,
        "added": added,
        "removed": removed,
        "skipped_skeleton": skipped_skeleton,
        "failed": failed,
        "restart_required": not reconciled,
    }


# ── Server instance ─────────────────────────────────────────────────────

# Middleware registration is a first-class constructor parameter in SDK v2
# (no private-attribute appends). The two middlewares only observe or
# refuse — see middleware.py for the rationale.
mcp = MCPServer(
    name="ppsspp-dfx-mcp",
    version=__version__,
    # The always-on usage briefing: injected into the model's context at
    # initialize and read before any tool is called. It therefore carries only
    # **cross-tool** knowledge (call order, session_id rules, the address trap,
    # context budget, first move after an error) — never a restatement of what
    # a tool's own description already says. Rationale and content rules live
    # with the text in `instructions.py`.
    instructions=INSTRUCTIONS,
    lifespan=_lifespan,
    # NOTE: no OpenTelemetry middleware is passed here — `MCPServer` already
    # installs it as a built-in, and user middleware runs INSIDE it
    # (mcpserver/server.py:242: "User middleware runs inside the SDK's
    # built-ins (OpenTelemetry, then the ...)"). Passing our own would
    # duplicate every span. Spans become observable once the embedding
    # application configures `opentelemetry-sdk`; the default provider is
    # NoOpTracerProvider.
    # Innermost-last: `tool_error_middleware` runs AFTER the rate limiter so
    # a rate-limited call is still refused with RATE_LIMIT_EXCEEDED, and its
    # short-circuited [ARGS_INVALID] result passes back out through both
    # observers unchanged.
    middleware=[request_id_middleware, rate_limit_middleware, tool_error_middleware],
)


# Annotations for dynamic script tools (aggregate STATE-CHANGE default).
_DEFAULT_SCRIPT_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)


def registered_tool_names() -> set[str]:
    """Names of all registered tools (static + dynamic script tools).

    Single point of access to the SDK's tool registry. The `_tool_manager`
    attribute is private SDK API (verified against mcp 2.1.1/2.2.0); if a future SDK
    removes it, switch to `asyncio.run(mcp.list_tools())` here — callers
    elsewhere in the codebase only use this function.
    """
    return set(mcp._tool_manager._tools.keys())  # noqa: SLF001 — private SDK API, isolated here


def registered_tool_count() -> int:
    """Count of all registered tools (static + dynamic script tools)."""
    return len(registered_tool_names())
