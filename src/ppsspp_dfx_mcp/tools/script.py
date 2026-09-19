"""Script tools — ppsspp_list_scripts / ppsspp_run_script / ppsspp_reload_scripts.

3 tools exposed:
- ppsspp_list_scripts(category?) — list manifest entries
- ppsspp_run_script(name, input) — invoke a script's `run(input, ctx)`
- ppsspp_reload_scripts() — re-read manifest YAML

Contract: each script declares
    class <Name>Input(BaseModel): ...
    class <Name>Output(BaseModel): ...
    async def run(input: <Name>Input, ctx: ScriptContext) -> <Name>Output: ...

`ScriptContext` provides:
- `logger`  — per-script logger (named `ppsspp_dfx_mcp.script.<name>`)
- `project_root` — Path to project root (cwd, no parent walking)
- `addresses` — dict from addresses.yaml (config.addresses())
- `session_id` — optional session ID (None for requires_ppsspp=False scripts)
- `request_id` — opaque correlation ID (string)
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import logging
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from ppsspp_dfx_mcp.config import addresses as _addresses
from ppsspp_dfx_mcp.errors import (
    ArgsInvalid,
    ManifestError,
    ScriptContractError,
    ScriptNotFound,
    SessionNotFound,
    ToolError,
    to_tool_error,
)
from ppsspp_dfx_mcp.server import mcp
from ppsspp_dfx_mcp.spec.script_manifest import (
    ScriptEntry,
    VALID_SCRIPT_CATEGORIES,
    get_manifest,
)
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views._contract import derive_output_contract
from ppsspp_dfx_mcp.views.script import (
    ReloadScriptsOutput,
    ScriptEntryView,
    ScriptListOutput,
    ScriptRunOutput,
)

ScriptListOutputContract = derive_output_contract("ScriptListOutputContract", ScriptListOutput)
ScriptRunOutputContract = derive_output_contract("ScriptRunOutputContract", ScriptRunOutput)
ReloadScriptsOutputContract = derive_output_contract(
    "ReloadScriptsOutputContract", ReloadScriptsOutput
)

logger = logging.getLogger(__name__)

__all__ = [
    "ScriptContext",
    "list_scripts",
    "run_script",
    "reload_scripts",
    "validate_script_contract",
]


# ── Script IoC context ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScriptContext:
    """Context injected into each script's `run(input, ctx)`.

    Frozen to make instances safe to share across coroutines. The session
    manager is NOT a frozen field by default — scripts that need an
    active PPSSPP connection must call `ppsspp_session(action=get,
    session_id=...)` themselves (single entry point, no implicit state).

    Attributes:
        logger: per-script logger; scripts should use this for diagnostics.
        project_root: Path to the project root (cwd per config.py policy).
        addresses: dict from addresses.yaml (game-specific constants).
        session_id: optional session ID (None for offline scripts).
        request_id: opaque correlation ID, unique per script invocation.
    """

    logger: logging.Logger
    project_root: Path
    addresses: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)


# ── Module loading cache ──────────────────────────────────────────────────
#
# We cache loaded modules by absolute path so a hot-loop of
# `ppsspp_run_script(name=...)` calls doesn't re-import the script each
# time. Cache is invalidated on `ppsspp_reload_scripts()`. We deliberately
# do NOT use sys.modules for caching because script paths are not on
# sys.path (and we don't want to pollute sys.modules with potentially
# colliding names like `state` or `misc`).
#
# A module-level `threading.Lock` guards `_module_cache` reads/writes so
# that the get-then-set sequence is atomic across threads (P1-8). On a
# single event loop `exec_module` is sync and never yields, so the lock
# does not affect async concurrency; it only prevents two threads from
# concurrently executing the same script's top-level code.

_module_cache: dict[str, Any] = {}
_module_cache_lock = threading.Lock()


def _clear_module_cache() -> None:
    """Drop all cached script modules + sys.modules entries we created.

    Called from `reload_scripts()` (and from test fixtures). Also pops
    the `_ppsspp_dfx_script_*` entries we inserted into `sys.modules`
    so long-running processes don't accumulate dead entries (P1-9).
    """
    _module_cache.clear()
    keys_to_remove = [k for k in sys.modules if k.startswith("_ppsspp_dfx_script_")]
    for k in keys_to_remove:
        del sys.modules[k]


def _load_script_module(entry: ScriptEntry, project_root: Path) -> Any:
    """Import the script module referenced by `entry`.

    Uses `importlib.util.spec_from_file_location` so the script file does
    NOT need to be on sys.path. Cached by absolute path string. The
    get-then-set cache sequence is guarded by `_module_cache_lock` so
    that two threads cannot race and execute the same module's top-level
    code twice (P1-8).

    Raises:
        ScriptContractError: file missing, import error, or entry function
            not callable / not async.
    """
    abs_path = entry.normalized_path(project_root)
    cache_key = str(abs_path)
    with _module_cache_lock:
        cached = _module_cache.get(cache_key)
        if cached is not None:
            return cached

        if not abs_path.exists():
            raise ScriptContractError(f"script file not found for {entry.name!r}: {abs_path}")

        # Use a unique module name to avoid collisions with sys.modules
        # entries like `state` or `misc` (which collide with stdlib /
        # package names).
        module_name = f"_ppsspp_dfx_script_{entry.name}"
        spec = importlib.util.spec_from_file_location(module_name, abs_path)
        if spec is None or spec.loader is None:
            raise ScriptContractError(
                f"cannot load script module for {entry.name!r} from {abs_path}"
            )
        module = importlib.util.module_from_spec(spec)
        # Populate sys.modules so relative imports inside the script work
        # (though we don't expect any — scripts should be self-contained).
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            sys.modules.pop(module_name, None)
            raise ScriptContractError(f"script {entry.name!r} import failed: {e}") from e

        # Verify entry function exists + is callable.
        fn = getattr(module, entry.entry, None)
        if fn is None:
            raise ScriptContractError(
                f"script {entry.name!r} missing entry function {entry.entry!r}"
            )
        if not callable(fn):
            raise ScriptContractError(
                f"script {entry.name!r} entry {entry.entry!r} is not callable"
            )

        _module_cache[cache_key] = module
        return module


def _get_input_output_models(module: Any, entry: ScriptEntry) -> tuple[type, type]:
    """Locate the Pydantic Input/Output model classes on `module`.

    Raises:
        ScriptContractError: class missing or not a Pydantic BaseModel.
    """
    input_cls = getattr(module, entry.input_model, None)
    output_cls = getattr(module, entry.output_model, None)
    if input_cls is None:
        raise ScriptContractError(
            f"script {entry.name!r} missing input model class {entry.input_model!r}"
        )
    if output_cls is None:
        raise ScriptContractError(
            f"script {entry.name!r} missing output model class {entry.output_model!r}"
        )
    if not (isinstance(input_cls, type) and issubclass(input_cls, BaseModel)):
        raise ScriptContractError(
            f"script {entry.name!r} input_model {entry.input_model!r} is not a Pydantic BaseModel"
        )
    if not (isinstance(output_cls, type) and issubclass(output_cls, BaseModel)):
        raise ScriptContractError(
            f"script {entry.name!r} output_model {entry.output_model!r} is not a Pydantic BaseModel"
        )
    return input_cls, output_cls


def validate_script_contract(entry: ScriptEntry, project_root: Path) -> tuple[Any, type, type, Any]:
    """Validate that a script entry's contract is well-formed.

    Combines `_load_script_module` + `_get_input_output_models` + entry
    function checks into a single helper so that lifespan pre-flight and
    `run_script` share the same contract validation logic (P1-11).

    Args:
        entry: manifest entry to validate.
        project_root: project root for resolving `entry.path`.

    Returns:
        (module, input_cls, output_cls, fn) on success.

    Raises:
        ScriptContractError: file missing, import error, Input/Output
            model missing or not Pydantic BaseModel, entry not callable
            or not an async function.
    """
    module = _load_script_module(entry, project_root)
    input_cls, output_cls = _get_input_output_models(module, entry)
    fn = getattr(module, entry.entry, None)
    if fn is None or not callable(fn):
        raise ScriptContractError(
            f"Entry '{entry.entry}' not found or not callable in {entry.path}",
            code="INVALID_ENTRY",
        )
    if not inspect.iscoroutinefunction(fn):
        raise ScriptContractError(
            f"Entry '{entry.entry}' must be async function",
            code="INVALID_ENTRY",
        )
    return module, input_cls, output_cls, fn


def _project_root() -> Path:
    """Project root is cwd (mirrors `config._project_root()`)."""
    return Path.cwd()


def _build_ctx(entry: ScriptEntry, session_id: str | None) -> ScriptContext:
    """Construct a ScriptContext for the given entry."""
    return ScriptContext(
        logger=logging.getLogger(f"ppsspp_dfx_mcp.script.{entry.name}"),
        project_root=_project_root(),
        addresses=_addresses(),
        session_id=session_id,
    )


# ── Tools ─────────────────────────────────────────────────────────────────


# Former docstring (kept as comment; description is now the TDQS docstring):
# List diagnostic scripts declared in the manifest.
#
# Returns:
# ScriptListOutput dict: scripts + count + category.
@mcp.tool(
    name="ppsspp_list_scripts",
    annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
def list_scripts(
    category: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional category filter. Valid values: "
                "eboot / state / p0ab / ndx / memory / misc / recipe. "
                "If omitted, all manifest entries are returned."
            ),
        ),
    ] = None,
) -> ScriptListOutputContract:
    """PURPOSE: List diagnostic scripts declared in .ppsspp-dfx/config/scripts.manifest.yaml.

    USAGE: category optional filter (eboot / state / p0ab / ndx / memory / misc / recipe).

    BEHAVIOR: READ-ONLY. Reads the in-memory manifest registry (loaded at startup). Does not execute any script.

    RETURNS: {scripts: [ScriptEntryView...], count, category}.
    """
    logger.info("tool_call", extra={"tool": "ppsspp_list_scripts", "category": category})
    if category is not None and category not in VALID_SCRIPT_CATEGORIES:
        # M12: an unknown category used to silently return an empty list —
        # indistinguishable from "category exists but has no scripts".
        # Fail like list_addresses does for unknown sections, listing the
        # valid values so the caller can self-correct.
        raise ArgsInvalid(
            f"unknown category {category!r}; valid categories: "
            f"{sorted(VALID_SCRIPT_CATEGORIES)}"
        )
    try:
        manifest = get_manifest()
        entries = manifest.list_scripts(category=category)
    except ManifestError as e:
        raise to_tool_error(e) from e
    # F5 (review-r3): surface the ACTUAL dynamic-tool registration state
    # next to each exposed entry so declared-vs-registered drift (skipped
    # skeleton, contract failure, pending restart) is visible to agents.
    from ppsspp_dfx_mcp.server import registered_exposed_names

    return ScriptListOutput.from_entries(
        entries, category, exposed_registered_names=registered_exposed_names()
    ).model_dump(mode="json")


# Former docstring (kept as comment; description is now the TDQS docstring):
# Invoke a diagnostic script by name.
#
# The script's `run(input, ctx)` is awaited. `input` is validated
# against the script's Pydantic Input model; the return value is
# serialized from the script's Pydantic Output model.
#
# Raises:
# ToolError (ScriptNotFound): name not in manifest.
# ToolError (ScriptContractError): script file missing, import
# failed, or contract violation.
# ToolError: any exception raised by the script's `run()`.
@mcp.tool(
    name="ppsspp_run_script",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    ),
)
@translate_tool_errors
async def run_script(
    name: Annotated[
        str,
        Field(description="Script name (must appear in manifest)."),
    ],
    input: Annotated[
        dict[str, Any],
        Field(
            description=(
                "Script input as a JSON dict. Validated against the script's "
                "Pydantic Input model. Pass {} for scripts with no required "
                "fields."
            ),
        ),
    ] = None,
    session_id: Annotated[
        str | None,
        Field(
            default=None,
            description=(
                "Optional session ID. Enforced by this tool for scripts "
                "with requires_ppsspp=true (fails with SESSION_NOT_FOUND "
                "when none resolves). Priority: this parameter > an "
                "optional session_id field on the script's Input model."
            ),
        ),
    ] = None,
) -> ScriptRunOutputContract:
    """PURPOSE: Invoke a manifest-registered diagnostic script by name with validated input.

    USAGE: name (see ppsspp_list_scripts; skeleton scripts return not_implemented); input dict validated against the script's Pydantic model; session_id required when the script declares requires_ppsspp (missing → SESSION_NOT_FOUND).

    BEHAVIOR: STATE-CHANGE. Runs manifest-registered script code. Unknown names → SCRIPT_NOT_FOUND.

    RETURNS: {name, output, output_model}."""
    if input is None:
        input = {}
    logger.info(
        "tool_call",
        extra={"tool": "ppsspp_run_script", "script_name": name, "session_id": session_id},
    )
    try:
        manifest = get_manifest()
        entry = manifest.get_script(name)
        project_root = _project_root()
        # Script module import executes the script's top-level code
        # synchronously while holding the module-cache lock — run it in a
        # worker thread so a slow import can't freeze the event loop.
        module = await asyncio.to_thread(_load_script_module, entry, project_root)
        input_cls, output_cls = _get_input_output_models(module, entry)
        # M7: pydantic silently ignores unknown fields by default, so a
        # typo'd parameter key passed validation and was swallowed — the
        # caller believed the parameter took effect. Reject unknown keys
        # up front (field aliases still resolve via the model itself).
        unknown = set(input) - set(getattr(input_cls, "model_fields", {}))
        if unknown:
            raise ScriptContractError(
                f"script {name!r} input has unknown field(s): "
                f"{sorted(unknown)} — accepted fields: "
                f"{sorted(getattr(input_cls, 'model_fields', {}))}"
            )
        try:
            input_model = input_cls(**input)
        except Exception as e:
            raise ScriptContractError(f"script {name!r} input validation failed: {e}") from e

        fn = getattr(module, entry.entry)
        # F3 (review-r3): resolve the effective session and enforce
        # requires_ppsspp HERE instead of leaving it to each script.
        # Priority: explicit tool parameter > Input-model session_id
        # field (the exposed wrapper forwards the same value via both
        # channels, so this resolution is transparent to that path).
        effective_session_id = session_id or getattr(input_model, "session_id", None)
        if entry.requires_ppsspp and not effective_session_id:
            raise SessionNotFound(
                f"script {name!r} requires an active PPSSPP session "
                f"(requires_ppsspp=true) but no session_id resolved; "
                f"start one first with ppsspp_session(action='start', "
                f"iso_path=...) and pass its session_id"
            )
        ctx = _build_ctx(entry, effective_session_id)
        try:
            result = await fn(input_model, ctx)
        except ToolError:
            # Don't re-wrap ToolError — `to_tool_error(e) from e` would
            # set `e.__cause__ = e` (self-reference cycle) because
            # `to_tool_error` returns the same ToolError instance (P1-10).
            raise
        except Exception as e:
            raise ScriptContractError(f"Script '{name}' raised: {e}", code="CONTRACT") from e

        if not isinstance(result, output_cls):
            raise ScriptContractError(
                f"script {name!r} returned {type(result).__name__}; expected {entry.output_model}"
            )
    except ScriptNotFound as e:
        raise to_tool_error(e) from e
    except ScriptContractError as e:
        raise to_tool_error(e) from e
    except ManifestError as e:
        raise to_tool_error(e) from e

    return ScriptRunOutput(
        name=name,
        output=result.model_dump(mode="json"),
        output_model=entry.output_model,
    ).model_dump(mode="json")


# Former docstring (kept as comment; description is now the TDQS docstring):
# Manually reload the manifest YAML.
#
# Idempotent: re-reading an unchanged file produces an equal registry.
# Also clears the script module cache so code edits are picked up on
# the next `ppsspp_run_script` call.
#
# Returns:
# ReloadScriptsOutput dict: reloaded_count + exposed_count +
# manifest_path + scripts.
@mcp.tool(
    name="ppsspp_reload_scripts",
    annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
    ),
)
async def reload_scripts(ctx: Context | None = None) -> ReloadScriptsOutputContract:
    """PURPOSE: Manually reload the script manifest YAML and clear the script module cache.

    USAGE: No parameters.

    BEHAVIOR: MUTATING. Re-reads the manifest file and invalidates cached script modules. Reversible: re-reading an unchanged file produces an equal registry. When the exposed tool set ACTUALLY changes (tools added or removed), the server notifies the client with a tool-list-changed notification so cached `tools/list` results are invalidated; a no-op reload sends nothing.

    RETURNS: {reloaded_count, exposed_count, manifest_path, scripts: [ScriptEntryView...]}.
    """
    logger.info("tool_call", extra={"tool": "ppsspp_reload_scripts"})
    # Clear module cache FIRST so that even if `manifest.reload()` raises
    # ManifestError, stale script modules don't linger in the cache (P1-7).
    # A subsequent successful reload will re-populate the cache on demand.
    _clear_module_cache()
    try:
        manifest = get_manifest()
        count = manifest.reload()
        entries = manifest.list_scripts()
        exposed = [e for e in entries if e.exposed]
    except ManifestError as e:
        raise to_tool_error(e) from e

    # F4 (review-r3): reconcile the dynamic tool registry with the
    # reloaded manifest — register newly exposed scripts, unregister
    # removed/reclassified ones — so edits no longer require a restart.
    from ppsspp_dfx_mcp.server import registered_exposed_names, sync_exposed_tools

    sync_report = sync_exposed_tools()

    # Dynamic tool registration is runtime-mutable, so a client's cached
    # `tools/list` can silently go stale after a manifest edit. Notify
    # ONLY when the exposed set actually changed — the tool advertises
    # `idempotentHint=True` and a no-op reload must stay side-effect free.
    #
    # The notification is **best-effort and must never fail the call**
    # (design D8：握手时代的能力不可达，本通知本就是尽力而为的额外项)：
    #   * `Context.request_context` raises outside a real request — e.g.
    #     `MCPServer.call_tool(name, args)` builds a Context with no request
    #     context (`mcpserver/server.py:540`), so an in-process call that
    #     DOES change the exposed set would raise *after* the registry was
    #     already mutated, reporting failure for a successful reload.
    #   * a transport failure must not turn a completed reload into an error.
    changed = bool(sync_report["added"] or sync_report["removed"])
    if changed and ctx is not None:
        try:
            await ctx.request_context.session.send_tool_list_changed()
        except Exception as e:  # noqa: BLE001 — 通知是额外项，任何失败都不该冒泡
            logger.warning(
                "tool_list_changed notification failed (reload itself succeeded): %s",
                e,
                extra={"tool": "ppsspp_reload_scripts"},
            )
    if changed:
        logger.info(
            "tool_list_changed",
            extra={
                "tool": "ppsspp_reload_scripts",
                "added": sync_report["added"],
                "removed": sync_report["removed"],
            },
        )

    return ReloadScriptsOutput(
        reloaded_count=count,
        exposed_count=len(exposed),
        manifest_path=str(manifest.manifest_path().resolve()),
        scripts=[
            ScriptEntryView.from_entry(
                e,
                exposed_registered=(
                    e.name in sync_report["added"]
                    or (
                        e.name in registered_exposed_names()
                        and e.name not in sync_report["removed"]
                    )
                ),
            )
            for e in entries
        ],
        exposed_registered=sync_report["registered"],
        exposed_added=sync_report["added"],
        exposed_removed=sync_report["removed"],
        restart_required=sync_report["restart_required"],
    ).model_dump(mode="json")
