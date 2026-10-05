from __future__ import annotations

from ppsspp_dfx_mcp.config import (
    configure_logging,
    rate_limit,
    validate_config,
    ws_host,
    ws_port,
)
from ppsspp_dfx_mcp.registry import (  # noqa: F401 — 向后兼容再导出（T051 前消费方仍从这里取）
    _DEFAULT_SCRIPT_ANNOTATIONS,
    _build_exposed_wrapper,
    _config_project_root,
    _exposed_registry,
    _exposed_registry_lock,
    _lifespan,
    _load_manifest_and_register_exposed,
    _register_exposed_entry,
    _shutdown_sessions,
    _unregister_exposed_tool,
    log,
    mcp,
    registered_exposed_names,
    registered_tool_count,
    registered_tool_names,
    sync_exposed_tools,
)

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

# Static tool modules are DISCOVERED by scanning the `tools` package
# (W25): a hand-written basename list was a second source of truth that had
# to be kept in sync on every new tool. Importing each module executes its
# `@mcp.tool()` decorators; Python's module cache makes repeated calls
# idempotent. Names starting with `_` (helpers like `_common`) are skipped,
# as is `smoke` (a manual probe with no `@mcp.tool`); iteration is sorted so
# registration order is deterministic (the 2026-07-28 spec asks for a
# deterministic tools/list order).
_SKIPPED_TOOL_MODULES: frozenset[str] = frozenset({"smoke"})


def _tool_module_names() -> list[str]:
    """Names of the importable static tool modules under `tools/`.

    Discovered via `pkgutil.iter_modules` (directory scan) and returned in
    sorted order. Private modules (`_`-prefixed) and the manual `smoke`
    probe are excluded.
    """
    import pkgutil

    from ppsspp_dfx_mcp import tools as _tools_package

    return sorted(
        name
        for _, name, _ in pkgutil.iter_modules(_tools_package.__path__)
        if not name.startswith("_") and name not in _SKIPPED_TOOL_MODULES
    )


def register_all_tools() -> int:
    """Import every static tool module, triggering decorator registration.

    Idempotent (module cache). Returns the number of registered tools
    after import (static only — dynamic script tools register in lifespan).
    """
    import importlib

    for mod in _tool_module_names():
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
