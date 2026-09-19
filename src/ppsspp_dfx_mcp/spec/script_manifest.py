"""Script manifest — single source of truth for script discovery.

Loads `.ppsspp-dfx/config/scripts.manifest.yaml` at server startup,
validates each entry with Pydantic, and exposes lookup APIs for
`ppsspp_list_scripts` / `ppsspp_run_script` / `ppsspp_reload_scripts`.

Design (see specs/ppsspp-dfx-mcp-script-manifest/spec.md):
- Manifest path resolution: `config.config_dir() / "scripts.manifest.yaml"`.
- Missing manifest → warning + empty list (server still starts).
- Malformed manifest → `ManifestError` raised at load time.
- No watchdog: callers invoke `reload()` to pick up edits.
- Reload is idempotent (re-reading the same file produces an equal registry).
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ppsspp_dfx_mcp.config import config_dir
from ppsspp_dfx_mcp.errors import ManifestError, ScriptNotFound

log = logging.getLogger(__name__)

__all__ = ["ScriptEntry", "ScriptManifest", "get_manifest"]


# Valid category values (single source of truth; mirrored in manifest YAML).
VALID_SCRIPT_CATEGORIES: frozenset[str] = frozenset(
    {"eboot", "state", "p0ab", "ndx", "memory", "misc", "recipe"}
)

# Machine-readable availability statuses (F1, review-r3):
# - "migrated": contract converted; `run(input, ctx)` is real logic.
# - "skeleton": contract converted but body returns not_implemented.
# `exposed=true` + status="skeleton" is rejected by the exposed preflight
# (server.py) so unusable capabilities are never registered as tools.
_VALID_STATUSES: frozenset[str] = frozenset({"migrated", "skeleton"})

# Description prefix that legacy manifests used to encode availability
# before the `status` field existed. Entries without an explicit `status`
# are inferred from this prefix so old manifests keep loading unchanged.
_SKELETON_DESCRIPTION_PREFIX = "[skeleton]"

_MANIFEST_FILENAME = "scripts.manifest.yaml"


class ScriptEntry(BaseModel):
    """Manifest entry describing one diagnostic script.

    Attributes:
        name: unique script identifier; also used in `ppsspp_script_<name>`.
        description: human-readable purpose (surfaced in tools/list).
        category: eboot / state / p0ab / ndx / memory / misc / recipe.
        requires_ppsspp: true if `run(input, ctx)` needs an active session.
        path: project-root-relative Python source path.
        input_model: Pydantic BaseModel class name declared in the script.
        output_model: Pydantic BaseModel class name declared in the script.
        entry: async function name (default "run").
        exposed: true → dynamically registered as `ppsspp_script_<name>`.
        status: migrated | skeleton (machine-readable availability; entries
            without an explicit status inherit it from the legacy
            description prefix, defaulting to migrated).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # W15 (review v2): the name is interpolated into tool names
    # (ppsspp_script_<name>), sys.modules keys and TypedDict names —
    # constrain it to the shape the server can actually use.
    name: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1)
    category: str = Field(min_length=1)
    requires_ppsspp: bool = False
    path: str = Field(min_length=1)
    input_model: str = Field(min_length=1)
    output_model: str = Field(min_length=1)
    entry: str = Field(default="run")
    exposed: bool = Field(default=False)
    status: str = Field(default="migrated")

    def normalized_path(self, project_root: Path) -> Path:
        """Resolve `path` against `project_root` (no parent walking).

        Contract: `path` is project-root-relative. We do NOT call
        `.resolve()` here to keep tests deterministic — callers that
        need an absolute path can resolve themselves.

        Escape hatch: if `path` is already absolute, it is returned
        as-is. This exists primarily so test fixtures can inject
        absolute paths without first copying files under a fake
        project root. Production manifests should always use relative
        paths; the absolute-path branch is DEPRECATED and may be
        removed once all call sites (including tests) are migrated
        to relative paths.
        """
        p = Path(self.path)
        if p.is_absolute():
            # Deprecated escape hatch (tests / workspace-rewired dev
            # scripts live outside a temp project root). Not containment
            # checked against project_root by design — but `..` still has
            # no business in a sanctioned entry, and the deprecation
            # should be VISIBLE so relative paths stay the norm.
            if ".." in p.parts:
                raise ManifestError(f"script path must not contain '..': {self.path}")
            if p.suffix != ".py":
                raise ManifestError(f"script path must point at a .py file: {self.path}")
            return p
        resolved = project_root / p
        # W15 (review v2): relative paths are containment-checked — a
        # tampered manifest must not reach outside the project root via
        # `..` (same defense shape as tools/_common.resolve_output_path).
        if ".." in p.parts or not resolved.resolve().is_relative_to(project_root.resolve()):
            raise ManifestError(f"script path escapes project root: {self.path}")
        return resolved


class ScriptManifest:
    """In-memory registry of script entries loaded from YAML.

    Thread-safe singleton: a single instance is shared across the MCP
    server's lifespan. Mutations (`reload()`) acquire a lock to keep
    `list_scripts()` / `get_script()` atomic w.r.t. reload.
    """

    def __init__(self, manifest_path: Path | None = None) -> None:
        """Construct manifest.

        Args:
            manifest_path: explicit path to manifest YAML. If None, uses
                `config_dir() / "scripts.manifest.yaml"` (lazy — read on
                first `_load()` call so test fixtures can set env vars).
        """
        self._explicit_path = manifest_path
        self._lock = threading.RLock()
        self._entries: list[ScriptEntry] = []
        self._by_name: dict[str, ScriptEntry] = {}
        self._loaded: bool = False

    # ── Public API ────────────────────────────────────────────────────────

    def manifest_path(self) -> Path:
        """Resolve the manifest YAML path.

        Priority:
        1. Explicit path passed to __init__ (test injection).
        2. `config_dir() / scripts.manifest.yaml` (env-overridable).
        """
        if self._explicit_path is not None:
            return self._explicit_path
        return config_dir() / _MANIFEST_FILENAME

    def is_loaded(self) -> bool:
        """True if `_load()` has been called at least once."""
        with self._lock:
            return self._loaded

    def ensure_loaded(self) -> None:
        """Lazily load on first access; idempotent thereafter."""
        with self._lock:
            if not self._loaded:
                self._load_locked()

    def reload(self) -> int:
        """Re-read the manifest YAML and rebuild the registry.

        Idempotent: re-reading an unchanged file produces an equal
        registry. Returns the new entry count.

        Raises:
            ManifestError: YAML is malformed or entries fail Pydantic
                validation.
        """
        with self._lock:
            self._load_locked()
            return len(self._entries)

    def list_scripts(self, category: str | None = None) -> list[ScriptEntry]:
        """Return manifest entries, optionally filtered by category.

        Args:
            category: if non-None, restrict to entries with matching
                category (case-sensitive). Unknown categories return an
                empty list (no error).

        Raises:
            ManifestError: if the manifest is loaded but malformed
                (propagated from `ensure_loaded()`).
        """
        self.ensure_loaded()
        with self._lock:
            entries = list(self._entries)
        if category is None:
            return entries
        return [e for e in entries if e.category == category]

    def get_script(self, name: str) -> ScriptEntry:
        """Return the entry for `name`.

        Raises:
            ScriptNotFound: name not in manifest.
            ManifestError: manifest malformed (propagated from
                `ensure_loaded()`).
        """
        self.ensure_loaded()
        with self._lock:
            entry = self._by_name.get(name)
        if entry is None:
            raise ScriptNotFound(f"Script not found: {name}")
        return entry

    def list_exposed(self) -> list[ScriptEntry]:
        """Return entries with `exposed=True` (auto-registered as tools)."""
        self.ensure_loaded()
        with self._lock:
            return [e for e in self._entries if e.exposed]

    # ── Internal ──────────────────────────────────────────────────────────

    def _load_locked(self) -> None:
        """Load (or reload) the manifest YAML. Caller holds `_lock`."""
        path = self.manifest_path()
        if not path.exists():
            log.warning("manifest not found at %s; scripts list will be empty", path)
            self._entries = []
            self._by_name = {}
            self._loaded = True
            return

        try:
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise ManifestError(f"manifest YAML parse failed: {e}") from e
        except OSError as e:
            raise ManifestError(f"manifest read failed: {e}") from e

        if not isinstance(data, dict):
            raise ManifestError(f"manifest root must be a mapping, got {type(data).__name__}")

        raw_scripts: Any = data.get("scripts", [])
        if not isinstance(raw_scripts, list):
            raise ManifestError(
                f"manifest 'scripts' must be a list, got {type(raw_scripts).__name__}"
            )

        entries: list[ScriptEntry] = []
        by_name: dict[str, ScriptEntry] = {}
        # Track input_model / output_model usage for duplicate detection.
        # Sharing models across scripts is allowed (a common Input type
        # may legitimately be reused), so duplicates produce a warning,
        # not an error (P2-14).
        by_input_model: dict[str, str] = {}
        by_output_model: dict[str, str] = {}
        for idx, raw in enumerate(raw_scripts):
            if not isinstance(raw, dict):
                raise ManifestError(
                    f"manifest scripts[{idx}] must be a mapping, got {type(raw).__name__}"
                )
            # F1 status backfill: entries without an explicit `status`
            # inherit it from the legacy description prefix so old
            # manifests keep loading unchanged (review-r3 migration path).
            if "status" not in raw:
                description = raw.get("description", "")
                inferred = (
                    "skeleton"
                    if isinstance(description, str)
                    and description.startswith(_SKELETON_DESCRIPTION_PREFIX)
                    else "migrated"
                )
                raw = {**raw, "status": inferred}
            try:
                entry = ScriptEntry(**raw)
            except ValidationError as e:
                raise ManifestError(f"manifest scripts[{idx}] validation failed: {e}") from e
            if entry.category not in VALID_SCRIPT_CATEGORIES:
                raise ManifestError(
                    f"manifest scripts[{idx}] name={entry.name!r} "
                    f"has invalid category={entry.category!r}; "
                    f"expected one of {sorted(VALID_SCRIPT_CATEGORIES)}"
                )
            if entry.status not in _VALID_STATUSES:
                raise ManifestError(
                    f"manifest scripts[{idx}] name={entry.name!r} "
                    f"has invalid status={entry.status!r}; "
                    f"expected one of {sorted(_VALID_STATUSES)}"
                )
            if entry.exposed and entry.status == "skeleton":
                log.warning(
                    "manifest scripts[%d] name=%r is exposed but status=skeleton; "
                    "the exposed preflight will NOT register it as a tool "
                    "(unusable capabilities must not surface in tools/list)",
                    idx,
                    entry.name,
                )
            if entry.name in by_name:
                raise ManifestError(f"manifest duplicate script name: {entry.name!r}")
            # Soft-check model uniqueness. Sharing is permitted but
            # usually indicates copy-paste; warn so authors can confirm
            # the sharing is intentional (P2-14).
            prev_input = by_input_model.get(entry.input_model)
            if prev_input is not None:
                log.warning(
                    "manifest scripts[%d] name=%r reuses input_model=%r "
                    "already declared by name=%r; sharing is allowed but "
                    "ensure this is intentional",
                    idx,
                    entry.name,
                    entry.input_model,
                    prev_input,
                )
            else:
                by_input_model[entry.input_model] = entry.name
            prev_output = by_output_model.get(entry.output_model)
            if prev_output is not None:
                log.warning(
                    "manifest scripts[%d] name=%r reuses output_model=%r "
                    "already declared by name=%r; sharing is allowed but "
                    "ensure this is intentional",
                    idx,
                    entry.name,
                    entry.output_model,
                    prev_output,
                )
            else:
                by_output_model[entry.output_model] = entry.name
            entries.append(entry)
            by_name[entry.name] = entry

        self._entries = entries
        self._by_name = by_name
        self._loaded = True
        log.info(
            "manifest loaded: %d scripts (%d exposed) from %s",
            len(entries),
            sum(1 for e in entries if e.exposed),
            path,
        )


# ── Module-level singleton ────────────────────────────────────────────────
#
# A single manifest instance is shared across the MCP server's lifespan.
# Tests can call `get_manifest().reload()` after pointing env vars at a
# fixture manifest; production callers should never construct their own
# ScriptManifest unless they need explicit isolation.

_singleton: ScriptManifest | None = None
_singleton_lock = threading.Lock()


def get_manifest() -> ScriptManifest:
    """Return the process-wide ScriptManifest singleton."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = ScriptManifest()
        return _singleton


def reset_manifest_for_tests(manifest: ScriptManifest | None = None) -> None:
    """Replace the singleton (tests only).

    Pass None to clear the singleton so the next `get_manifest()` call
    constructs a fresh one. Pass a ScriptManifest to inject a custom
    instance (e.g. with an explicit path).
    """
    global _singleton
    with _singleton_lock:
        _singleton = manifest
