"""The bundled skill must describe the server's real surface.

``skills/ppsspp-dfx/`` is what an agent reads before it decides what to call.
Nothing tied it to the server, so it drifted silently in both directions —
measured 2026-10-03, four separate classes:

- one registered tool (``ppsspp_watch_value``) had **no row at all**, so an
  agent could not discover the zero-pause alternative to a memory watchpoint;
- one live error code (``CAPTURE_EMPTY``) was missing from the table that
  calls itself the complete list — while the SKILL summary *and* a playbook
  both mentioned it, i.e. the "complete" table was less complete than the
  summary it claims to extend;
- the scan row still described pre-guard behaviour (``background=true``,
  "24MB about 40s") after the auto-backgrounding guard shipped, so the doc
  advertised a call shape that now silently changes meaning;
- two files restated a protected-code-section bound the implementation had
  already replaced with a runtime extent measured from the session.

Each is a *class* of drift, not a one-off, so this module binds the
machine-checkable ones to their single sources of truth:

- tool coverage      -> live ``tools/list``
- error codes        -> ``spec.error_codes.ERROR_CODE_TO_CLASS``
- retired names      -> the v0.1.6 retirement list (mapping in CHANGELOG.md)
- version narrative  -> forbidden in skill prose

This is a documentation guard, not a behaviour test. It fails when the skill
and the implementation disagree, because that is the only way an agent can be
sent after a tool that cannot be called, or miss one that can.

Scope (T065 extended): BOTH bundled skills — the generic ``skills/ppsspp-dfx/``
AND the project-side ``skills/ppsspp-dfx-topx/`` — are swept by the shared
detectors (retired names, version narrative, frontmatter header). The topx
skill carries addresses and game-specific facts, which have no machine
source here, so the *tool-coverage* and *error-code* detectors still bind
only to the generic skill; the prose-discipline detectors bind to both.
One detector implementation, two roots — no second copy of the rules.

Run (from the repository root):
    <venv python> -m pytest tests/unit/l2_mcp_contract/test_skill_surface_consistency.py -q
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

_MCP_ROOT = Path(__file__).resolve().parents[3]
_REPO_ROOT = _MCP_ROOT.parent.parent
_SKILL_DIR = _MCP_ROOT / "skills" / "ppsspp-dfx"
#: T065: the project-side skill joins the prose-discipline guards below.
_PROJECT_SKILL_DIR = _REPO_ROOT / "skills" / "ppsspp-dfx-topx"
_TOOL_SURFACE = _SKILL_DIR / "references" / "tool-surface.md"
_ERROR_CODES = _SKILL_DIR / "references" / "error-codes.md"

#: Names that match ``ppsspp_*`` but are not callable tools. Each is a real
#: non-tool use of the prefix; the list is an allowlist so a *new* stray name
#: fails the test instead of being waved through by a loose regex.
#:
#: - ``ppsspp_dfx_mcp``  the Python package / server name
#: - ``ppsspp_exe``      the ``project.yaml`` config key for the emulator path
#: - ``ppsspp_log``      the package logger name
#: - ``ppsspp_addr``     a variable in the address-arithmetic formula
#: - ``ppsspp_script_``  the dynamic tool namespace, registered from
#:   ``scripts.manifest.yaml``; dynamic members are not in the static surface
_NON_TOOL_PREFIXES = (
    "ppsspp_dfx_mcp",
    "ppsspp_exe",
    "ppsspp_log",
    "ppsspp_addr",
    "ppsspp_script_",
)

#: Tool names retired by the v0.1.6 surface merge (mapping table in
#: CHANGELOG.md). A surviving mention advertises a tool that cannot be called.
_RETIRED_TOOL_NAMES = (
    "ppsspp_get_pc",
    "ppsspp_smoke_test",
    "ppsspp_dump_texture",
    "ppsspp_dump_clut",
    "ppsspp_session_list",
    "ppsspp_convert_address",
    "ppsspp_wait_breakpoint",
    "ppsspp_trace_memory_access",
)

_TOOL_TOKEN_RE = re.compile(r"\bppsspp_[a-z0-9_]+")
_VERSION_TOKEN_RE = re.compile(r"\bv\d+\.\d+(?:\.\d+)?\b")
_DATE_STAMP_RE = re.compile(r"\b20\d\d-\d\d-\d\d\b")


# ── sources of truth ────────────────────────────────────────────────────


def _registered_tools() -> dict[str, Any]:
    """The live static tool surface, exactly as the server publishes it."""
    from ppsspp_dfx_mcp import server as srv

    srv.register_all_tools()
    return {t.name: t for t in asyncio.run(srv.mcp.list_tools())}


def _skill_markdown() -> dict[str, str]:
    """Every Markdown file in BOTH bundled skills, keyed by ``<skill>/<path>``.

    T065: the project-side skill (``ppsspp-dfx-topx``) shares the same
    prose-discipline detectors — one implementation, two roots.
    """
    docs: dict[str, str] = {}
    for root in (_SKILL_DIR, _PROJECT_SKILL_DIR):
        for p in sorted(root.rglob("*.md")):
            docs[f"{root.name}/{p.relative_to(root).as_posix()}"] = p.read_text(encoding="utf-8")
    return docs


def _strip_frontmatter(text: str) -> str:
    """Document body only — ``metadata.version`` is where a version belongs."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    return text[end + 4 :] if end != -1 else text


def _tool_tokens(text: str) -> set[str]:
    """``ppsspp_*`` tokens in ``text``, minus the documented non-tool uses."""
    tokens = set(_TOOL_TOKEN_RE.findall(text))
    return {t for t in tokens if not any(t == p or t.startswith(p) for p in _NON_TOOL_PREFIXES)}


def _business_error_codes() -> tuple[str, ...]:
    """Registered business error codes, from their single source of truth."""
    from ppsspp_dfx_mcp.spec.error_codes import ERROR_CODE_TO_CLASS

    return tuple(sorted(ERROR_CODE_TO_CLASS))


@pytest.fixture(scope="module")
def tools() -> dict[str, Any]:
    return _registered_tools()


@pytest.fixture(scope="module")
def markdown() -> dict[str, str]:
    return _skill_markdown()


@pytest.fixture(scope="module")
def tool_surface() -> str:
    return _TOOL_SURFACE.read_text(encoding="utf-8")


# ── tool coverage ───────────────────────────────────────────────────────


def test_tool_surface_is_not_vacuous(tool_surface: str, tools: dict[str, Any]) -> None:
    """Guard against the extractors silently matching nothing.

    Every assertion below is "for each X, X appears in the doc". If the doc
    stops being readable (renamed, emptied, encoding change) they would all
    pass while checking nothing — the failure mode this whole module exists
    to prevent, so it gets its own assertion.
    """
    assert len(tools) >= 30, f"tool surface collapsed to {len(tools)} tools?"
    assert tool_surface.count("| `ppsspp_") >= 30, (
        "tool-surface.md no longer looks like a tool table "
        f"({tool_surface.count('| `ppsspp_')} rows found)"
    )


@pytest.mark.parametrize("name", sorted(_registered_tools()))
def test_every_registered_tool_is_documented(name: str, tool_surface: str) -> None:
    """A tool an agent cannot find in the skill is a tool it will not use.

    This is how ``ppsspp_watch_value`` stayed invisible: the surface grew, the
    table did not, and no check noticed.
    """
    assert re.search(rf"\b{re.escape(name)}\b", tool_surface), (
        f"{name} is registered but absent from references/tool-surface.md. "
        f"Add a row in the same commit as the tool."
    )


def test_tool_surface_names_only_real_tools(tool_surface: str, tools: dict[str, Any]) -> None:
    """The reverse direction: a named tool must be callable.

    Either it exists, or the name belongs in ``_NON_TOOL_PREFIXES`` with a
    reason. A retired name that survives teaches the agent a call that fails.
    """
    ghosts = sorted(_tool_tokens(tool_surface) - set(tools))
    assert not ghosts, (
        f"references/tool-surface.md names {ghosts}, which the server does "
        f"not register. Either they are retired (drop the name) or they are "
        f"not tools at all (add to _NON_TOOL_PREFIXES with a reason)."
    )


# ── error codes ─────────────────────────────────────────────────────────


def test_error_codes_table_is_not_vacuous() -> None:
    text = _ERROR_CODES.read_text(encoding="utf-8")
    assert text.count("| `") >= 20, "error-codes.md no longer looks like a table?"


@pytest.mark.parametrize("code", _business_error_codes())
def test_every_business_error_code_is_documented(code: str) -> None:
    """The table calls itself complete; hold it to that.

    ``CAPTURE_EMPTY`` was produced by the server, named in the SKILL summary
    and in a playbook, and missing from the "complete" list.
    """
    text = _ERROR_CODES.read_text(encoding="utf-8")
    assert f"`{code}`" in text, (
        f"{code} is a registered business error code but is not documented in "
        f"references/error-codes.md. Add a row in the same commit."
    )


def test_reserved_codes_are_accounted_for() -> None:
    """A code that is deliberately *not* in the table must say so.

    ``PPSSPP_ERROR`` is the abstract base of its family and is never raised
    directly. Leaving it silently missing makes "the table is complete" and
    "the table forgot one" indistinguishable to a reader.
    """
    from ppsspp_dfx_mcp.spec.error_codes import RESERVED_CODES

    text = _ERROR_CODES.read_text(encoding="utf-8")
    undocumented = sorted(c for c in RESERVED_CODES if f"`{c}`" not in text)
    assert not undocumented, (
        f"reserved code(s) {undocumented} are neither tabulated nor explained. "
        f"Name them in the coverage note so their absence is deliberate."
    )


# ── retired names and version narrative ─────────────────────────────────


@pytest.mark.parametrize("rel", sorted(_skill_markdown()))
def test_no_retired_tool_names(rel: str) -> None:
    hits = sorted(n for n in _RETIRED_TOOL_NAMES if n in _skill_markdown()[rel])
    assert not hits, (
        f"{rel} references retired tool name(s) {hits}. Use the current tool "
        f"(mapping table in CHANGELOG.md, v0.1.6 surface merge)."
    )


@pytest.mark.parametrize("rel", sorted(_skill_markdown()))
def test_no_version_narrative_in_skill_prose(rel: str) -> None:
    """The body states current rules only; history lives in git.

    A reader who is told "this changed in v0.1.6" has to work out which half
    of the sentence is still true. Version history belongs in
    ``metadata.version`` and the changelog.
    """
    body = _strip_frontmatter(_skill_markdown()[rel])
    found = sorted(set(_VERSION_TOKEN_RE.findall(body)) | set(_DATE_STAMP_RE.findall(body)))
    assert not found, (
        f"{rel} body contains version/date narrative {found}. State the "
        f"current rule; keep the history in metadata.version + git."
    )


# ── documentation header ────────────────────────────────────────────────

#: Fields every reference/playbook doc carries. Re-homed from the retired
#: ``ppsspp-dfx-skill-doc-header`` capability spec: that spec's *body* rules
#: (playbook sections, type x category table, ``link_start.md`` merge)
#: described a layout the monorepo split replaced, but its frontmatter rule
#: still held and nothing else enforced it — ``scripts/analyze/audit_docs.py``
#: scans ``docs/`` only. Retiring the spec without moving the rule here would
#: have silently dropped it.
_FRONTMATTER_REQUIRED = ("title", "type", "category")


@pytest.mark.parametrize("rel", sorted(_skill_markdown()))
def test_reference_docs_carry_a_header(rel: str) -> None:
    """Every bundled doc declares its header up front.

    ``SKILL.md`` has a different contract: MCP requires ``name`` /
    ``description`` and forbids nothing else, so it is asserted rather than
    skipped — the fix for a failing header must never be to add a skip.
    """
    text = _skill_markdown()[rel]
    assert text.startswith("---"), f"{rel}: missing YAML frontmatter"
    end = text.find("\n---", 3)
    assert end != -1, f"{rel}: frontmatter is not closed with '---'"
    frontmatter = text[3:end]
    required = ("name", "description") if rel.endswith("SKILL.md") else _FRONTMATTER_REQUIRED
    missing = [key for key in required if not re.search(rf"^{key}:", frontmatter, re.M)]
    assert not missing, (
        f"{rel}: frontmatter missing {missing}. SKILL.md carries the MCP "
        f"fields; every other bundled doc declares title / type / category so "
        f"a reader can place it."
    )


# ── self-check: the checks above must be able to fail ───────────────────


class TestTheChecksCanFail:
    """Prove each detector fires on the shape it is meant to catch.

    Without this the module could pass by matching nothing, and a future
    refactor could quietly turn a check into a no-op.
    """

    def test_missing_tool_row_is_detectable(self, tools: dict[str, Any]) -> None:
        sample = next(iter(tools))
        fake = "# tool table with no rows\n"
        assert not re.search(rf"\b{re.escape(sample)}\b", fake), (
            "the presence check would not have caught a missing row"
        )

    def test_ghost_name_is_detectable(self, tools: dict[str, Any]) -> None:
        fake = "| `ppsspp_retired_thing` | gone |"
        assert _tool_tokens(fake) - set(tools) == {"ppsspp_retired_thing"}

    def test_allowlist_survives_the_ghost_check(self, tools: dict[str, Any]) -> None:
        fake = "the `ppsspp_script_x` namespace and `ppsspp_exe` config key"
        assert not (_tool_tokens(fake) - set(tools)), (
            "documented non-tool uses must not be reported as ghosts"
        )

    def test_version_narrative_is_detectable(self) -> None:
        assert _VERSION_TOKEN_RE.findall("merged in v0.1.6 for good")
        assert _DATE_STAMP_RE.findall("measured 2026-10-03 on the image")

    def test_frontmatter_version_is_not_prose(self) -> None:
        text = "---\nmetadata:\n  version: 3.0.1\n---\nbody text\n"
        assert not _VERSION_TOKEN_RE.findall(_strip_frontmatter(text))
