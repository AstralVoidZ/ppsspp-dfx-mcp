"""MCP server entry — SDK v2 decorator registration path.

High-level server is `MCPServer` (the SDK v2 rename of FastMCP); static
tools self-register via `@mcp.tool()` decorators in `tools/*.py`, and
`register_all_tools()` (called from `main()` and tests) triggers the
imports. Dynamic `ppsspp_script_<name>` tools are registered in
lifespan via `mcp.add_tool()` (still the supported dynamic path).

Static tool inventory: the committed baseline
`tests/unit/l2_mcp_contract/tool_surface_baseline.json` is the single
source of truth for the tool count and per-tool surface (name /
description / annotations). Counts are deliberately NOT restated in
prose so they cannot drift from the registry; the L2 contract tests
enforce both the baseline (same-commit regeneration rule) and the
ToolAnnotations set.

Run with: python -m ppsspp_dfx_mcp
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Annotated, Any, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp import __version__
from ppsspp_dfx_mcp.config import (
    configure_logging,
    rate_limit,
    validate_config,
    ws_host,
    ws_port,
)
from ppsspp_dfx_mcp.instructions import INSTRUCTIONS
from ppsspp_dfx_mcp.middleware import (
    rate_limit_middleware,
    request_id_middleware,
)
from ppsspp_dfx_mcp.session import session_manager

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
    _load_manifest_and_register_exposed()
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
        output_cls: Pydantic BaseModel subclass for the script's output
            (currently only used in the wrapper docstring for clarity).

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
            default = field_info.default
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

        # F2 (review-r3): forward session_id to run_script so the exposed
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
    # F-10 (2026-09-08): the overrides above used to DROP the return
    # annotation — and the SDK only emits structuredContent when it can
    # infer an output schema from `inspect.signature(fn, eval_str=True)`.
    # With no return annotation the wrapper's tools answered in the text
    # channel only (verified: func_metadata(wrapper).output_schema was
    # None while decorator-registered tools got one). Keep the return
    # annotation in BOTH override channels.
    #
    # tool-schema-contract (2026-09-13): the annotation is now a real
    # contract instead of a bare `dict[str, Any]` — a single static
    # annotation could only ever say "arbitrary object".
    #
    # The contract describes what the wrapper ACTUALLY returns, which is
    # `run_script()`'s envelope `{name, output, output_model}` — NOT the
    # script's Output model by itself. The first version of this change
    # derived the contract straight from `output_cls`, which was wrong in a
    # way the test suite could not see: the SDK validates every returned
    # dict against the derived contract
    # (`func_metadata.convert_result` → `validate_python`), so every
    # dynamic tool call failed with "N validation errors for
    # <name>OutputContract" (verified by calling ppsspp_script_hello_diagnostic
    # in-process). Nesting `output_cls` under the envelope's `output` field
    # keeps the script's field-level schema visible to the Agent AND matches
    # the real return shape.
    # 动态 TypedDict：envelope 名含运行时 entry.name，类语法无法表达
    # （UP013 的类转换仅适用于静态定义），故本行 noqa。
    output_contract = TypedDict(  # type: ignore[operator]  # noqa: UP013
        f"{entry.name}OutputContract",
        {
            "name": str,
            "output": output_cls,
            "output_model": str,
        },
    )
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


def _load_manifest_and_register_exposed() -> None:
    """Load the script manifest at startup and register exposed scripts.

    Best-effort: a missing or malformed manifest logs a warning and skips
    exposed-tool registration. The 3 base script tools
    (list_scripts/run_script/reload_scripts) remain registered regardless.

    F4 (review-r3): this is now the startup call of `sync_exposed_tools()`
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

    report = sync_exposed_tools()
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


# ── Exposed-tool registry (F4/F5, review-r3) ─────────────────────────────
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


def _register_exposed_entry(entry: Any, project_root: Any) -> bool:
    """Pre-flight + register one exposed script as a dynamic tool.

    Returns True when the tool was registered (or already present),
    False when skipped/failed. Skeleton-status entries are rejected
    before any import is attempted (F1) — unusable capabilities must
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
    try:
        _module, input_cls, output_cls, _fn = validate_script_contract(entry, project_root)
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
        mcp.add_tool(
            _exposed_wrapper,
            name=tool_name,
            description=(
                f"Diagnostic script '{entry.name}': {entry.description} "
                f"(category={entry.category})."
            ),
            annotations=_DEFAULT_SCRIPT_ANNOTATIONS,
        )
    except Exception as e:
        log.warning("failed to register exposed tool %r: %s", tool_name, e)
        return False

    with _exposed_registry_lock:
        _exposed_registry[entry.name] = tool_name
    return True


def _unregister_exposed_tool(script_name: str) -> bool:
    """Remove a dynamic `ppsspp_script_*` tool from the SDK registry.

    Returns True when removed (or already absent). Uses the same
    private SDK registry as `registered_tool_names()`.
    """
    with _exposed_registry_lock:
        tool_name = _exposed_registry.pop(script_name, None)
    if tool_name is None:
        return True
    removed = mcp._tool_manager._tools.pop(tool_name, None) is not None  # noqa: SLF001 — private SDK API, isolated here
    if not removed:
        log.warning(
            "exposed tool %r was not present in the SDK registry (already removed?)",
            tool_name,
        )
    return True


def sync_exposed_tools() -> dict[str, Any]:
    """Reconcile the dynamic tool registry with the manifest.

    Desired set = manifest entries with exposed=true AND status=migrated
    (F1: skeleton entries are never registered). Registers missing tools,
    unregisters stale ones, and returns a report dict:

        {declared, registered, added, removed, skipped_skeleton, failed,
         restart_required}

    `restart_required` stays False in the normal path; it flips True only
    when an add/remove raised unexpectedly AND the registry could not be
    reconciled — callers surface it so agents know to restart the server.
    """
    from pathlib import Path

    from ppsspp_dfx_mcp.spec.script_manifest import get_manifest

    manifest = get_manifest()
    manifest.ensure_loaded()
    exposed = manifest.list_exposed()
    project_root = Path.cwd()

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
        if _register_exposed_entry(entry, project_root):
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
    return {
        "declared": len(exposed),
        "registered": registered,
        "added": added,
        "removed": removed,
        "skipped_skeleton": skipped_skeleton,
        "failed": failed,
        "restart_required": False,
    }


# ── Server instance ─────────────────────────────────────────────────────

# Middleware registration is a first-class constructor parameter in SDK v2
# (no private-attribute appends). The two middlewares only observe or
# refuse — see middleware.py D5 rationale.
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
    middleware=[request_id_middleware, rate_limit_middleware],
)


# ── Registration helpers ────────────────────────────────────────────────

# Core liveness-critical tools that must always be registered at startup.
# The full tool set is registered via register_all_tools(); startup
# validation only checks this core subset — adding new tools does NOT
# require bumping a "phase counter" or updating expected sets.
_CORE_TOOLS: frozenset[str] = frozenset(
    {
        "ppsspp_health",
        "ppsspp_session",
    }
)

# Static tool modules imported by `register_all_tools()`; importing each
# module executes its `@mcp.tool()` decorators. Python's module cache makes
# repeated calls idempotent.
_TOOL_MODULE_NAMES: tuple[str, ...] = (
    "analyze",
    "assemble",
    "batch_step",
    "breakpoint",
    "context",
    "diff",
    "evaluate",
    "gpu_record",
    "gpu_stats",
    "input",
    "introspect",
    "list_addresses",
    "memory",
    "search_memory_info",
    "memory_map",
    "query",
    "replay",
    "screenshot",
    "script",
    "search_disasm",
    "session",
    "smoke",
    "state_observer",
    "step",
    "workflows",
    "write_register",
)

# Annotations for dynamic script tools (aggregate STATE-CHANGE default).
_DEFAULT_SCRIPT_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)


def register_all_tools() -> int:
    """Import every static tool module, triggering decorator registration.

    Idempotent (module cache). Returns the number of registered tools
    after import (static only — dynamic script tools register in lifespan).
    """
    import importlib

    for mod in _TOOL_MODULE_NAMES:
        importlib.import_module(f"ppsspp_dfx_mcp.tools.{mod}")
    # Resources + Prompts (delivery U-02): snapshot resources and the
    # memory-breakpoint-wizard prompt register via decorators on import.
    # They do not appear in the tool registry (registered_tool_count).
    # Completions (openspec `prompt-argument-completions`) must be imported
    # after prompts only by convention — it references prompt names by
    # string, not by object.
    importlib.import_module("ppsspp_dfx_mcp.resources")
    importlib.import_module("ppsspp_dfx_mcp.prompts")
    importlib.import_module("ppsspp_dfx_mcp.completions")
    return registered_tool_count()


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


def _assert_core_tools() -> None:
    """Verify the core tool subset is registered at startup.

    Only checks that the core liveness-critical tools (health / session)
    are present — additional tools are registered by
    `register_all_tools()` before this check, and dynamic script tools
    are registered in lifespan.

    Design rationale: tool registration order is driven by inter-tool
    dependencies (declared via imports inside tool functions), not by
    development phase. A "phase counter" that must be bumped on every
    tool addition is an anti-pattern — it couples runtime validation
    to development history and accumulates deprecated phase constants.
    The core subset check is sufficient: if `register_all_tools()` ran,
    all registered tools are available; if it didn't, the core subset
    will be missing and startup fails loudly.

    Pure sync: reads the tool registry directly (mcp.list_tools requires
    an event loop).
    """
    registered = registered_tool_names()
    missing = _CORE_TOOLS - registered
    if missing:
        raise RuntimeError(
            f"core tools missing from registry: {sorted(missing)} got={sorted(registered)}"
        )
    log.info(
        "ppsspp-dfx-mcp ready: %d tools registered (core subset ok)",
        len(registered),
    )


def main() -> None:
    """Server entry point."""
    validate_config()
    configure_logging()
    log.info(
        "starting ppsspp-dfx-mcp (ws=%s:%d, rate_limit=%d/min)",
        ws_host(),
        ws_port(),
        rate_limit(),
    )
    register_all_tools()
    _assert_core_tools()
    mcp.run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
