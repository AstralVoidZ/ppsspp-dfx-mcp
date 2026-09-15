# Contributing to ppsspp-dfx-mcp

Thanks for your interest in improving the project. This document defines the
documentation and comment conventions every source file must follow, plus the
practical notes for getting a development environment running.

## Development setup

The server requires an isolated venv: it imports MCP SDK v2
(`mcp.server.mcpserver`), which cannot coexist with the 1.x `mcp` package that
many other MCP servers pin.

```bash
# From the repository root — creates .venv/ppsspp-dfx-mcp and installs
# (editable, with dev extras):
python scripts/check_env.py --bootstrap

# Verify interpreter / SDK version / package import:
python scripts/check_env.py --check

# Run the test suite:
.venv/ppsspp-dfx-mcp/Scripts/python -m pytest tests -q    # Windows
.venv/ppsspp-dfx-mcp/bin/python -m pytest tests -q        # POSIX
```

## Module docstrings (file headers)

Every module opens with a short docstring that answers **what this is, why it
exists (only if non-obvious), and the key contract or invariant** a reader
must know before changing it. Rules:

1. **Length**: aim for ≤ 10 lines. A header that needs more than that usually
   means the module is doing too much.
2. **No history.** Never reference when or why a change happened, no dates,
   no change IDs (`W1 fix`, `D-19`, `R15`, `建议3`, `review issue #N`,
   `spike U2`, `acceptance 7`, `Phase 2`, `A1/A2`…). That information lives in
   version control.
3. **No dead links.** Reference only documentation that ships with the
   project (this file, `tests/`, tool docstrings). Do not link to workspace
   planning/analysis documents — if the underlying fact matters, state the
   fact itself in one line instead.
4. **Tool modules** list the tools they expose with one line each.

Example:

```python
"""Memory tool wrappers.

3 tools exposed:
- ppsspp_read_memory(action, ...) — aggregate read (bytes/u32/string/scan)
- ppsspp_write_memory(address, data, format?) — write u32 or bytes
- ppsspp_disassemble(address, count?) — disassemble N instructions

Reads and writes go through the session's debug client; protected ranges
(kernel, top.prx code) reject writes unless force=True.
"""
```

## Inline comments

A comment earns its place only when it states a constraint the code cannot
show: a non-obvious invariant, a race condition, a protocol quirk, an order of
operations that must not change.

1. **No changelog comments.** Same rule as headers — no fix IDs, no dates, no
   issue numbers, no "this used to be X". If the old behaviour matters, the
   constraint it produced is what you write down.
2. **Explain why, not what.** `# increment retry counter` is noise;
   `# the probe must run before the handshake or PPSSPP answers with an
   empty frame` is a comment.
3. Placement: own line above the code it governs.

## Tool docstrings are protocol surface

The docstring of each tool function in `src/ppsspp_dfx_mcp/tools/` is the
description the MCP client shows to models. It follows the
PURPOSE / USAGE / BEHAVIOR / RETURNS convention and is locked by a contract
test against a committed baseline. **Do not reword tool docstrings** in
refactors: changing them changes the tool surface every client sees. Treat
them like a public API.

## Tests

- New behaviour needs a test next to the layer it belongs to
  (`tests/unit/…` mirroring `src/`, contract tests in
  `tests/unit/l2_mcp_contract/`).
- Test names describe the behaviour they lock in; the module docstring may
  summarise what a test file locks in, under the same no-history rules.

## Pull requests

- Keep the change surgical: every line should trace back to the purpose of
  the PR.
- Run the full test suite before pushing.
- If a change touches tool signatures, descriptions, or output contracts,
  regenerate the tool-surface baseline
  (`scripts/dump_tool_surface.py`) **in the same commit** and say so in the
  description.

## File naming

- `scripts/` executables: verb-first snake_case — `<verb>_<object>.py`
  (`dump_tool_surface.py`, `check_env.py`, `record_fixtures.py`).
  Private helpers take a leading underscore (`_wire.py`); data files keep a
  descriptive name with their real extension.
- `evals/` modules: snake_case, one purpose per module; tests follow
  `test_<subject>.py`.
- Evaluation reports are development-process artifacts and are NOT
  committed to this repository: `report-<topic>-<YYYYMMDD>.md`.

## Test layers

`tests/unit` is organized in four layers — place a new test by what it
locks, not by where similar-looking code lives:

| Layer | Locks | A test belongs here when… |
|---|---|---|
| `unit/core` | Domain primitives | The pure function/class under `core/` (parsers, address math, path resolution) |
| `unit/l1_contract` | PPSSPP wire contract | The shape/semantics of a WS event as the transport sees it |
| `unit/l2_mcp_contract` | MCP tool surface | Tool signatures, descriptions, schemas, error codes, annotations |
| `unit/l3_orchestration` | Session/transport orchestration | Multi-component behaviour across session manager, client helper, observers |
| `unit/l4_regression` | One historical defect per file | A specific shipped bug must never reappear |

`tests/integration/` requires a real PPSSPP + ISO (auto-skips when absent);
its modules are marked `integration` + `real_ppsspp`.

## Testing conventions

- **Does-not-raise tests**: calling the function bare is accepted, but if it
  returns a value, assert it (`assert fn(...) is None`). A bare call cannot
  distinguish "returned None" from "returned garbage".
- **Patching `is_pid_alive`**: production reads it as
  `proc.is_pid_alive(...)` (module attribute) — patch
  `ppsspp_dfx_mcp.core.proc.is_pid_alive`, never a stale re-export.
- **Tool-surface baseline**: `tests/unit/l2_mcp_contract/tool_surface_baseline.json`
  snapshots the *static* registry (41 tools). Manifest scripts add dynamic
  `ppsspp_script_*` tools at runtime, so a live server's `tool_count` is
  baseline + manifest count.
