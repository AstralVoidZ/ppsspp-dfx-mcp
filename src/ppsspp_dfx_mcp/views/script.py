"""Script view — public JSON contract for ppsspp_list_scripts / run_script / reload_scripts.

3 tools:
- ppsspp_list_scripts → ScriptListOutput (entries + count)
- ppsspp_run_script   → ScriptRunOutput (name + output dict)
- ppsspp_reload_scripts → ReloadScriptsOutput (reloaded_count + manifest_path)
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.spec.script_manifest import ScriptEntry
from ppsspp_dfx_mcp.views._base import FrozenModel


class ScriptEntryView(FrozenModel):
    """Public-facing manifest entry (subset of ScriptEntry fields)."""

    name: str = Field(description="Unique script identifier.")
    description: str = Field(description="Human-readable purpose.")
    category: str = Field(description="eboot / state / p0ab / ndx / memory / misc / recipe.")
    requires_ppsspp: bool = Field(
        default=False,
        description="True if the script needs an active PPSSPP session.",
    )
    path: str = Field(description="Project-root-relative source path.")
    input_model: str = Field(description="Pydantic input model class name.")
    output_model: str = Field(description="Pydantic output model class name.")
    entry: str = Field(default="run", description="Async entry function name.")
    exposed: bool = Field(
        default=False,
        description="True → auto-registered as ppsspp_script_<name>.",
    )
    status: str = Field(
        default="migrated",
        description="migrated = runnable; skeleton = body returns not_implemented.",
    )
    exposed_registered: bool | None = Field(
        default=None,
        description=(
            "Actual dynamic-tool registration state. True/False only for "
            "exposed scripts when the registry is known; None otherwise "
            "(non-exposed entries, or registry not consulted)."
        ),
    )

    @classmethod
    def from_entry(
        cls, entry: ScriptEntry, exposed_registered: bool | None = None
    ) -> "ScriptEntryView":
        """Construct from a ScriptEntry domain model.

        `exposed_registered` carries the actual registration state (from
        the server's exposed registry); leave it None when the registry
        was not consulted (e.g. bare entry → view conversions in tests).
        """
        return cls(
            name=entry.name,
            description=entry.description,
            category=entry.category,
            requires_ppsspp=entry.requires_ppsspp,
            path=entry.path,
            input_model=entry.input_model,
            output_model=entry.output_model,
            entry=entry.entry,
            exposed=entry.exposed,
            status=entry.status,
            exposed_registered=exposed_registered if entry.exposed else None,
        )


class ScriptListOutput(FrozenModel):
    """Response view for ppsspp_list_scripts."""

    scripts: list[ScriptEntryView] = Field(
        default_factory=list,
        description="Manifest entries (filtered by category if requested).",
    )
    count: int = Field(default=0, description="Number of entries returned.")
    category: str | None = Field(
        default=None,
        description="Category filter applied (None = no filter).",
    )

    @classmethod
    def from_entries(
        cls,
        entries: list[ScriptEntry],
        category: str | None,
        exposed_registered_names: set[str] | None = None,
    ) -> "ScriptListOutput":
        """Build the list view.

        `exposed_registered_names` is the set of script names that are
        ACTUALLY registered as dynamic tools (server registry). When
        provided, each exposed entry's `exposed_registered` reflects
        reality — so a declared-but-unregistered (or registered-but-
        removed-from-manifest) exposed script is visible to agents
        instead of silently diverging (F5, review-r3).
        """
        names = exposed_registered_names
        return cls(
            scripts=[
                ScriptEntryView.from_entry(
                    e,
                    exposed_registered=(names is not None and e.name in names),
                )
                for e in entries
            ],
            count=len(entries),
            category=category,
        )


class ScriptRunOutput(FrozenModel):
    """Response view for ppsspp_run_script.

    `output` is the script's Pydantic Output model serialized to a JSON-safe
    dict (`model_dump(mode="json")`). The MCP client sees a plain dict; the
    Pydantic class name is echoed for type introspection.
    """

    name: str = Field(description="Script name that was executed.")
    output: dict[str, Any] = Field(
        default_factory=dict,
        description="Script output (serialized Pydantic Output model).",
    )
    output_model: str = Field(
        description="Pydantic Output model class name (for type introspection).",
    )


class ReloadScriptsOutput(FrozenModel):
    """Response view for ppsspp_reload_scripts."""

    reloaded_count: int = Field(
        description="Total script entries after reload (manifest-wide).",
    )
    exposed_count: int = Field(
        description="Number of exposed scripts (exposed=true) after reload.",
    )
    manifest_path: str = Field(description="Absolute path to the manifest YAML.")
    scripts: list[ScriptEntryView] = Field(
        default_factory=list,
        description="All entries after reload (post-reload snapshot).",
    )
    exposed_registered: int = Field(
        default=0,
        description=(
            "Number of exposed scripts ACTUALLY registered as dynamic "
            "tools after the reload sync (F4/F5: declared != registered "
            "is now surfaced instead of silently diverging)."
        ),
    )
    exposed_added: list[str] = Field(
        default_factory=list,
        description="Script names newly registered by this reload's sync.",
    )
    exposed_removed: list[str] = Field(
        default_factory=list,
        description="Script names unregistered by this reload's sync.",
    )
    restart_required: bool = Field(
        default=False,
        description=(
            "True when some registration changes could not be applied "
            "at runtime (SDK limitation) and a server restart is needed "
            "to fully reconcile exposed tools."
        ),
    )
