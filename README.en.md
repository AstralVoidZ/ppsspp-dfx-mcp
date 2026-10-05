# ppsspp-dfx-mcp

English | [中文](README.md)

[![PyPI](https://img.shields.io/pypi/v/ppsspp-dfx-mcp)](https://pypi.org/project/ppsspp-dfx-mcp/)
[![CI](https://github.com/AstralVoidZ/ppsspp-dfx-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/AstralVoidZ/ppsspp-dfx-mcp/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.13%2B-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)


A [MCP (Model Context Protocol)](https://modelcontextprotocol.io) server that turns
[PPSSPP](https://www.ppsspp.org/) into an AI-debuggable target. It wraps the PSP
emulator's WebSocket debugger into a tool surface for LLM agents: session lifecycle,
memory read/write, disassembly, breakpoints, CPU control, input automation,
screenshots, replay recording, and diagnostic scripts — with structured contracts,
a defensive error taxonomy, and task-level evaluations built in.

Documentation: [docs/SCOPE.md](docs/SCOPE.md) (scope & protocol-surface
boundaries) · [CHANGELOG.md](CHANGELOG.md) (changelog)

## Project status

The project is in _alpha_ and iterating quickly — **the tool surface and
configuration format may change incompatibly**. Read [SECURITY.md](SECURITY.md)
before running.

## Features

- **37 static tools**, all with structured `inputSchema` / `outputSchema` — no
  unconstrained return values; every parameter is typed and documented.
- **Dynamic script tools**: project-specific diagnostic scripts are exposed as
  `ppsspp_script_<name>` tools via `scripts.manifest.yaml`, with input types
  driven by each script's Pydantic model; `ppsspp_run_script` invokes
  non-exposed scripts and `ppsspp_list_scripts` inspects the manifest — see
  [Configuration](#configuration).
- **Session model**: multiple concurrent PPSSPP sessions, readiness probing
  (`wait_ready`), and wedge self-healing (`resilient` start).
- **Agent ergonomics**: composite tools (`ppsspp_frame_snapshot`,
  `ppsspp_breakpoint(action="wait"/"trace")`, `ppsspp_batch_step`), `session_id`
  auto resolution, defensive error codes (`[CODE] message` format; CPU-freeze
  vs disconnect disambiguation), with recovery hints embedded in error text.
- **Background automation**: batch jobs run in detached server tasks,
  immune to MCP client tool-call timeouts; supports status polling,
  cancellation, and registry inventory (`ppsspp_batch_status` with `batch_id` omitted).
- **Built-in evaluations** (`evals/`): 49 scenario cards + deterministic gates
  + a blind-test runner over recorded fixtures + summary reports — the tool
  surface is tested the way agents actually use it.
- **Honest protocol surface**: capabilities are only declared when a working
  implementation exists behind them; deliberately `false` switches carry
  design rationale.

## Running

Requirements: Python 3.13+ (with a dedicated venv — why: see
[Run from source](#run-from-source)); a PPSSPP build with the WebSocket
debugger enabled — official builds work, see
[docs/ppsspp-build.md](docs/ppsspp-build.md) for the toggle (the server
launches it and connects to `ws://<host>:<port>/debugger`); any MCP client
(ZCode, Claude Desktop, MCP Inspector, ...).

### Install from PyPI

> **Version status**: PyPI carries an _alpha_-stage release snapshot and may
> lag the repository's `main`. Go by the wheel you actually installed
> (`pip show ppsspp-dfx-mcp`); see [CHANGELOG.md](CHANGELOG.md) for changes.

Use a dedicated venv — this server's MCP SDK v2 cannot coexist with the 1.x
`mcp` package many other MCP servers pin:

```bash
# Windows：
py -3.14 -m venv .venv
# POSIX：
python3.14 -m venv .venv
.venv\Scripts\python -m pip install ppsspp-dfx-mcp     # Windows
.venv/bin/python -m pip install ppsspp-dfx-mcp         # POSIX
```

Register the server with your MCP client (the entry point ships with the
package; no in-repo scripts needed):

```json
{
  "mcpServers": {
    "ppsspp-dfx": {
      "command": "C:/absolute/path/to/.venv/Scripts/ppsspp-dfx-mcp.exe",
      "cwd": "C:/absolute/path/to/your-project"
    }
  }
}
```

`command` points at the entry executable inside the install venv —
`.venv/bin/ppsspp-dfx-mcp` on POSIX. `cwd` is the directory where the server
discovers its `.ppsspp-dfx/` configuration (see [Configuration](#configuration)).

> **`cwd` is optional** — some harnesses reject the key. Setting the project
> root in `env.PPSSPP_DFX_PROJECT_ROOT` instead is equivalent: verified by
> launching from `%TEMP%` with no `cwd` and only that variable set, where the
> server still resolves `project_root` / `config_dir` / `output_dir` and
> completes the handshake. With neither, it degrades to a warning and loses
> `scripts.manifest.yaml`, so the `ppsspp_script_*` tools vanish silently.

Smoke-test manually:

```bash
.venv/Scripts/ppsspp-dfx-mcp.exe   # Windows
.venv/bin/ppsspp-dfx-mcp           # POSIX
```

Then simply hand tasks to your agent: *"boot the emulator with this ISO and
tell me the current PC"* — the server handles session startup, readiness
probing, and state reads. Tool descriptions follow the PURPOSE / USAGE /
BEHAVIOR / RETURNS convention with recovery guidance embedded in error paths;
agents are self-sufficient without examples.

> Do not insert wrapper scripts between the client and the server: on Windows
> `os.execv` is `CreateProcess` + parent wait (not POSIX exec-replacement), so
> an extra layer makes the innermost server read stdin EOF immediately and exit
> silently — the symptom is just `-32000: Connection closed`.

### Run from source

The repository checkout ships a bootstrap script and a ready-to-use `.mcp.json`:

```bash
git clone https://github.com/AstralVoidZ/ppsspp-dfx-mcp.git
cd ppsspp-dfx-mcp

# Run in this directory — creates .venv/ppsspp-dfx-mcp and installs
# (editable, with dev extras):
python scripts/check_env.py --bootstrap

# Verify interpreter / SDK version / package imports / every .mcp.json:
python scripts/check_env.py --check
```

`--bootstrap` provisions a venv beside **every** `.mcp.json` in the checkout
(the workspace root, plus the package dir `mcps/ppsspp-dfx-mcp/` in a
monorepo). Why: an MCP client resolves a relative `command` against the
**config's own directory**, and the repo policy forbids committing absolute
paths — so a committed config only resolves if a matching venv really sits next
to it. `--check` validates each config and fails on mismatch (the
package-local config drifted unpoliced).

`--bootstrap` prepares only the **main venv** (`.venv/ppsspp-dfx-mcp`, used by the
tests and the full-suite run). Launching the **server** no longer needs it: the
committed `.mcp.json` uses the general-runner form, and the runner provisions its
own environment on first run.

The two legal shapes of `.mcp.json` (both are checked by `--check`):

| Config location | Shape | Why |
|---|---|---|
| **Package root** (repo root of a standalone checkout) | `["run", "ppsspp-dfx-mcp"]` | Self-locating: the runner finds the project from the working directory |
| **Not the package root** (workspace root of the monorepo) | `["run", "--directory", "<package dir>", "ppsspp-dfx-mcp"]` | The package directory **must** be pinned, otherwise the workspace's own project is picked up |

**Working-directory assumption**: MCP clients normally spawn the server with
**the directory holding `.mcp.json`** as the working directory; the relative
form depends on that. `config.project_root()` resolves
`PPSSPP_DFX_PROJECT_ROOT` first and then the working directory, and it does
**not** search upwards — a working directory pointing elsewhere misresolves the
output directory and the script manifest (symptom: the dynamic script tools
silently disappear).

Do **not** put a platform-bound interpreter path (`…/Scripts/python.exe` or
`…/bin/python`) in a committed config: it necessarily fails on the other
operating system.

Register the server with your MCP client — point the client at the in-repo
`.mcp.json`, or inline it with the same structure (package-root case):

```json
{
  "mcpServers": {
    "ppsspp-dfx": {
      "command": "uv",
      "args": ["run", "ppsspp-dfx-mcp"]
    }
  }
}
```

If the client resolves `command` against a **different** working directory (i.e.
it does not honour the assumption above), use the fragment printed by
`python scripts/check_env.py --print-config` — it has the same runner shape but
pins the package directory by absolute path, ready to paste.
Also make sure the general runner is on `PATH` (`--check` verifies it; when it
is missing, an stdio client only sees the subprocess exit, never the reason).

If your client resolves a relative `command` against some other working
directory (not the config's directory), the relative snippet above is
unreliable. Use `--print-config` instead — it prints an absolute-path snippet
with `cwd` set to the workspace root, ready to paste:

```bash
python scripts/check_env.py --print-config
```

Manual start for verification:

```bash
.venv/ppsspp-dfx-mcp/Scripts/python -m ppsspp_dfx_mcp   # Windows
.venv/ppsspp-dfx-mcp/bin/python -m ppsspp_dfx_mcp        # POSIX
```

## Configuration

Environment variables (all optional):

| Variable | Default | Purpose |
|-----|---------|-------------|
| `PPSSPP_DFX_LOG_LEVEL` | `INFO` | Log level |
| `PPSSPP_DFX_LOG_FORMAT` | `text` | Log format (`text` or `json`) |
| `PPSSPP_DFX_RATE_LIMIT` | `60` | Per-tool rate limit (calls/min, 0 disables) |
| `PPSSPP_DFX_WS_HOST` | `127.0.0.1` | PPSSPP WebSocket host |
| `PPSSPP_DFX_WS_PORT` | `12345` | PPSSPP WebSocket port |
| `PPSSPP_DFX_EXE_PATH` | (from yaml) | PPSSPP executable path |
| `PPSSPP_DFX_SESSIONS_PATH` | `~/.ppsspp-dfx/sessions.json` | Session state path |
| `PPSSPP_DFX_PROJECT_ROOT` | `cwd` | Project root, overriding cwd discovery (set it when the MCP host spawns the server from a temp dir). A missing path raises `CONFIG_INVALID`; a valid path without the `.ppsspp-dfx/` marker dir only warns |
| `PPSSPP_DFX_CONFIG_DIR` | `<project_root>/.ppsspp-dfx/config` | Config dir, overriding the default discovery |
| `PPSSPP_DFX_IR` | unset | Set `1` to force `CPUCore=2` interpreter mode. **Required for memory breakpoints** — they do not fire under JIT fastmem. Costs speed; use for trace/breakpoint sessions |
| `PPSSPP_DFX_BOOT_HEAL_QUARANTINE` | `1` | `1` = let boot-wedge self-heal quarantine the GPU backend blacklist file (rename only, never delete); `0` = skip that step |
| `PPSSPP_DFX_MEMSTICK_DIR` | auto-detected | Memstick dir (log / screenshot capture) |
| `PPSSPP_DFX_WORKSPACE_ROOT` | auto-detected | Workspace root for `scripts/_wire.py` (nearest ancestor holding `.mcp.json`). Bootstrap scripts only |
| `PPSSPP_DFX_ALLOW_REMOTE_DEBUGGER` | unset | Set to `1` to accept a debugger bound to a non-loopback address (default: fail closed). See [SECURITY.md](SECURITY.md) |
| `PPSSPP_DFX_ALLOW_ABS_SCRIPT` | unset | Set to `1` to allow absolute paths in the script manifest (default: relative only). See [SECURITY.md](SECURITY.md) |
| `PPSSPP_DFX_ISO_ROOT` | unset | ISO path allowlist root — when set, `iso_path` must resolve inside that tree. See [SECURITY.md](SECURITY.md) |

> **`PPSSPP_DFX_WS_PORT` only applies when connecting to an already-running
> PPSSPP.** When the server launches PPSSPP itself it picks a random free port
> and discovers it post-launch (avoiding the 12345 conflict), so the variable
> is ignored in that flow.

Eval-only variables (needed only when running `evals/`):
`PPSSPP_DFX_TEST_MODE`, `PPSSPP_DFX_FIXTURE_DIR`, `PPSSPP_DFX_TEST_EXE_PATH`,
`PPSSPP_DFX_TEST_ISO_PATH`, `PPSSPP_DFX_TEST_PPSSPP_LOG`, `PPSSPP_DFX_SKILL_DIR`,
`PPSSPP_DFX_EVALS_LLM_API_PATH`, `PPSSPP_DFX_EVAL_GAME_*`.

A complete, copy-ready config template (every variable annotated, grouped by
purpose) is in [`examples/mcp.json.template`](examples/mcp.json.template).

Project-level YAML configuration lives in `.ppsspp-dfx/config/` (relative to
the working directory):

- `project.yaml` — `ppsspp_exe` path and project metadata
- `addresses.yaml` — named address constants (also feed the `completions`
  capability of the memory wizards)
- `scripts.manifest.yaml` — diagnostic script manifest. Each entry carries a
  machine-readable `status` (`migrated` = runnable, `skeleton` = body returns
  `not_implemented`). Scripts flagged `exposed: true` register as
  `ppsspp_script_<name>` tools at startup — skeletons excluded; preflight
  rejects them. `ppsspp_reload_scripts` re-syncs the dynamic tool registry
  with the manifest (no restart) and reports the reconciliation.

### Standalone quick start

The three config files (`project.yaml` / `addresses.yaml` /
`scripts.manifest.yaml`) ship as ready-to-copy templates — start there, don't
hand-write YAML from scratch. Where you get them depends on how you installed:

**From a source checkout** (the templates are in the repo):

```bash
mkdir -p .ppsspp-dfx/config
cp examples/project.yaml examples/addresses.yaml \
   examples/scripts.manifest.yaml .ppsspp-dfx/config/
```

**From PyPI** (the wheel packages only `src/ppsspp_dfx_mcp` and does **not**
contain `examples/` — fetch the same templates from GitHub):

```bash
mkdir -p .ppsspp-dfx/config
for f in project.yaml addresses.yaml scripts.manifest.yaml; do
  curl -fsSL "https://raw.githubusercontent.com/AstralVoidZ/ppsspp-dfx-mcp/main/examples/$f" \
    -o ".ppsspp-dfx/config/$f"
done
```

You can also browse/download them one by one from
[`examples/`](https://github.com/AstralVoidZ/ppsspp-dfx-mcp/tree/main/examples).
Once copied, edit `.ppsspp-dfx/config/project.yaml`: point `ppsspp_exe` at your
WS-debugger-enabled PPSSPP build, and replace the PLACEHOLDER addresses in
`addresses.yaml` with values you reverse-engineered for your own game.

Two things to know before the first session:

- Without `scripts.manifest.yaml` the server still starts, but all
  `ppsspp_script_*` tools silently disappear — keep the template even if the
  `scripts:` list is empty (`check_env.py --check` warns about exactly this).
- With empty config and no placeholder values, everything server-side works;
  only session startup needs a real `ppsspp_exe` (or `PPSSPP_DFX_EXE_PATH`),
  and address constants only matter once you provide your game's values.

## Protocol surface

Declared at `initialize` handshake — and **only** capabilities with a working
implementation behind them (the SDK derives each capability from whether a
request handler exists, so everything listed here is real):

| Capability | Declared | Notes |
|---|---------|-------|
| `tools` | ✅ | 37 static tools + dynamic `ppsspp_script_<name>` |
| `resources` | ✅ | `ppsspp://game-state`, `ppsspp://registers` (snapshots) |
| `prompts` | ✅ | `memory-breakpoint-wizard`, `memory-trace-wizard` |
| `completions` | ✅ | the wizards' `address` argument, candidates from `addresses.yaml` |
| `logging` | ❌ | protocol revision 2026-07-28 removed `logging/setLevel` |
| `tasks` | ❌ | SDK 2.2.0 type definitions only, no server-side implementation |

`tools.list_changed` and `resources.subscribe` are deliberately **`false`**.
SDK 2.2.0's `MCPServer` exposes no handshake-time entry to set
`notification_options`; declaring them would promise notifications the server
cannot emit. Current substitutes:

- `ppsspp_reload_scripts` **reports** what changed (`exposed_added` /
  `exposed_removed`) — agents respond without a notification channel.
- The server `instructions` string tells fresh agents what the tool surface
  contains.

If the SDK later exposes the entry point, flip the switch and add the
`send_*_list_changed` calls — the L2 contract tests
(`tests/unit/l2_mcp_contract/test_capabilities_contract.py`) assert the
current `false` state and will fail, which is the design signal that the
decision needs re-visiting, not a regression.

### Return shapes

**Image tools** (`ppsspp_screenshot`, `ppsspp_dump`) return a split `CallToolResult`:

- `content` — one `ImageContent` block carrying the pixels.
- `structuredContent` — metadata only (`file_path` / `size_bytes` / `format`,
  plus per-tool fields like `mode`, `width`, `height`, `empty`). The base64
  copy of the image is deliberately **not** in this channel — it would bloat
  the schema and duplicate what `content` already carries.

Every tool declares a structured `outputSchema` — no tool returns an
unconstrained object or an array with empty `items`. The one registered
exception is `ppsspp_run_script`'s `input` parameter: its shape is decided by
the invoked script, so it is described but not constrained.

## Error handling

When the emulated CPU wedges (infinite loop / HLE blocking / GPU pipeline
stall), the server returns `CPU_FREEZE_SUSPECTED` instead of a blanket
`WS_DISCONNECTED` — distinguishing "PPSSPP alive but CPU frozen" from
"process dead / WebSocket gone".

Recommended handling for `CPU_FREEZE_SUSPECTED`:

- **Do not** restart the session — PPSSPP is still running.
- Take a screenshot with `ppsspp_screenshot` to aid diagnosis.
- Try `step(action='resume')` (may not help a true infinite loop).
- Inspect threads with `hle.thread.list` (may expose HLE blocking).
- Check the instruction stream at the current PC with `ppsspp_disassemble`.

Related codes: `WS_DISCONNECTED` (PID dead — real disconnect), `WS_TIMEOUT`
(ticketed RPC timeout, conservative default), `CPU_STATE_ERROR` (current CPU
state unsuitable for the operation). Error text always starts with `[CODE]`
for programmatic classification; recovery advice is embedded wherever a next
step exists.

### Troubleshooting quick reference

| Symptom | Cause / fix |
|---|---|
| `-32000: Connection closed` (no other info) | A wrapper script sits between the MCP client and the server: on Windows `os.execv` is really `CreateProcess` + parent wait (not POSIX exec-replacement), so the inner server's stdin hits EOF immediately and exits silently. Remove the middle layer and use the venv interpreter as `command` (see [Run from source](#run-from-source)) |
| Client fails to start the server with `-32000: Connection closed` / `command` path does not exist | The config's relative interpreter path was never provisioned (no venv beside it), or the client resolved the relative `command` against a different working directory. Run `python scripts/check_env.py --bootstrap` to create the venv beside each config, or paste the absolute-path snippet from `python scripts/check_env.py --print-config` (see [Run from source](#run-from-source)) |
| `check_env` reports "standalone venv missing" | `.venv/` is gitignored, so a fresh clone never has it. Run `python scripts/check_env.py --bootstrap` |
| `mcp SDK version unsatisfied` / import-time crash | The system Python's `mcp` package is often pinned to 1.x by other MCP servers — irreconcilable with SDK v2. Don't install globally — use `check_env.py --bootstrap`, or install into a dedicated venv per [Install from PyPI](#install-from-pypi) |
| All `ppsspp_script_*` tools vanish (server starts fine) | `.ppsspp-dfx/config/scripts.manifest.yaml` missing — absence only warns, dynamic tools silently empty. Follow [Standalone quick start](#standalone-quick-start) to fetch the three templates (`check_env.py --check` tells you; under a PyPI install they are not in the wheel, so fetch them from GitHub) |
| `[PPSSPP_NOT_FOUND]` | PPSSPP executable not configured. Set `PPSSPP_DFX_EXE_PATH`, or `ppsspp_exe` in `.ppsspp-dfx/config/project.yaml` (env > yaml precedence) |
| Can't find `.ppsspp-dfx/config` | The config dir resolves from CWD (no upward search). Start from a directory containing `.ppsspp-dfx/`, or set `PPSSPP_DFX_CONFIG_DIR` |
| WebSocket connect fails / `WS_DISCONNECTED` | PPSSPP not running, wrong port, or the WebSocket debugger isn't enabled. Verify with `check_env.py --check` and `ppsspp_session(action='get')` |
| Tool call hangs / times out (`WS_TIMEOUT`) | PPSSPP's main loop dispatches WebSocket requests: when the UI is frozen, a modal dialog is up, or emulation is paused, requests are not serviced. Screenshot first to check the UI state |
| `[BOOT_TIMEOUT]` during boot | Wedged-boot suspicion. `start(resilient=true)` quarantines the GPU-backend blacklist (renames `FailedGraphicsBackends.txt`, never deletes) and self-heals (≤2 retries) |

### `structuredContent` serialization caveat

Serialization behavior is pinned to **mcp SDK 2.2.0** (`mcp.server.mcpserver`)
as measured. Multi-shape tools (one tool, different response shapes per
`action` — e.g. `ppsspp_breakpoint` / `ppsspp_diff_memory` / `ppsspp_scan` /
`ppsspp_session` / `ppsspp_batch_step` / `ppsspp_frame_snapshot`) declare
their output contract as partial: the full payload always travels the
`content` text channel as JSON, while how far `structuredContent` is
populated for a given shape can differ across SDK versions — machine
consumers should fall back to the text-channel JSON.

### Known limitations

Honest statement of the protocol surface's boundaries — each item is also
annotated in the corresponding tool description; summarized here:

- **IR encoding cannot be reliably detected MCP-side**: reading a JIT-IR code
  section with `read_u32` does not error but may yield a meaningless value (not
  a real MIPS instruction) — read code sections with `ppsspp_disassemble`
  (`ppsspp_search_disasm` for instruction search).
- **Breakpoint conditions are evaluated MCP-side**: this build's IR mode ignores
  register conditions (upstream defect, verified on real hardware), so
  `breakpoint` never forwards `condition` to PPSSPP — `action='wait'` evaluates
  it with `cpu.evaluate` when the hit lands, and **the evaluator only runs while
  a `wait` is active**; falsy hits are auto-resumed and counted in
  `filtered_hits`, and ≥10 hits/<1s at one address trip the hit-storm breaker
  (auto-disarm + `storm_break=true`). If the CPU was already paused before
  arming, the pause cannot be attributed (a manual pause is indistinguishable
  from a hit): the call returns `hit=true` with a `note` stating that the
  registered condition was **not** evaluated.
- **No save-state API**: PPSSPP's WebSocket debugger exposes no `savestate.*`
  events; save/load cannot be provided. Use PPSSPP's UI hotkeys (F1–F8 slots).
- **Frame stepping is instruction-granular only**: `step` uses `cpu.stepInto`.
  Whole-frame alternative: breakpoint the vblank handler, then `resume`.
- **`trace` orchestrates memory breakpoints only**: `ppsspp_breakpoint(action='trace')`
  arms a memory-access breakpoint (read access by default). For a one-shot wait
  on an execution breakpoint, combine `action='set'` + `action='wait'`.
- **Analog stick is persistent shared state**: `send_analog` writes stick
  until the next write; no auto-reset.
- **VRAM direct-read screenshots are unreliable**: direct VRAM reads are not
  synchronized with GPU rendering; colors may be wrong. Default channel is
  `render`. `source='output'` can crash on some titles — use only as the
  render-channel empty-frame fallback.
- **Replay clock anchoring**: replay timelines use the recording session's
  absolute game clock from boot; injection only works after a fresh boot via
  the boot-aligned sequence (documented in the tool response).
- **Protected address ranges need explicit `force=true`**: kernel memory and
  the top.prx code section reject writes/assembly by default — a
  mis-write guard, not a limitation bug.
- **Session state is single-writer**: `~/.ppsspp-dfx/sessions.json` shares
  session registration across processes; concurrent MCP server instances
  pointed at the same path last-write-wins.

## Performance reference (measured locally)

Reference environment: Windows x64, PPSSPP v1.20.4-605, server and PPSSPP on
the same machine (localhost WS). Numbers vary with machine and game load —
use them to estimate timeout budgets, not as a performance promise:

| Operation | Measured |
|---|---|
| Single WS round-trip (lightweight call such as `game.status`) | p50 ≈ 0.21 ms, p95 ≈ 0.28 ms (n=60) |
| Full-band 24 MB pattern scan (`ppsspp_scan` `background=true`, 64 KiB chunks) | ≈ 40 s (384 chunked reads) |
| Breakpoint hit → observable (hot-address `set` + `wait`, from resume to confirmed wait) | p50 ≈ 11 ms (n=30) |

## Community & support

- Report bugs and feature requests via
  [GitHub Issues](https://github.com/AstralVoidZ/ppsspp-dfx-mcp/issues).
- For configuration and session problems, start with the
  [troubleshooting quick reference](#troubleshooting-quick-reference) and
  [known limitations](#known-limitations).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Development

Start from [docs/SCOPE.md](docs/SCOPE.md) (scope & protocol-surface
boundaries) and [evals/README.md](evals/README.md) (blind-test evaluation
system: scenario cards, deterministic gates, runner, reports).

```bash
# Prerequisite: pytest lives in the dev dependency group (not installed by
# default) — either of:
#   uv sync                                # installs the dependency group
#   pip install -e ".[dev]"                # or the dev extra
# Full test suite (unit + contract + integration; 1700+ test cases (excluding parametrize expansion)):
.venv/ppsspp-dfx-mcp/Scripts/python -m pytest tests -q

# Regenerate the tool-surface baseline after signature/description changes
# (commit together with the change):
.venv/ppsspp-dfx-mcp/Scripts/python scripts/dump_tool_surface.py
```

## Acknowledgements

- [PPSSPP](https://www.ppsspp.org/) — the debugged target itself. The
  WebSocket debugging protocol contract (`debugger.ppsspp.org` subprotocol,
  event semantics, HLE introspection fields) was mapped item-by-item against
  its source ([docs/SCOPE.md](docs/SCOPE.md)).
- [mcp-ppsspp](https://github.com/dmang-dev/mcp-ppsspp), mcp-bizhawk,
  mcp-mgba — comparable emulator-MCP bridges; this server's coverage
  positioning was benchmarked against them (see the comparison section in
  [docs/SCOPE.md](docs/SCOPE.md)).
- Runtime dependencies ([MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk),
  [pydantic](https://docs.pydantic.dev/), PyYAML, websockets) are declared in
  [pyproject.toml](pyproject.toml).

## Citation

```bibtex
@misc{ppssppdfxmcp2026,
  title={ppsspp-dfx-mcp: a PPSSPP debug MCP server for PSP game localization},
  author={AstralVoidZ and contributors},
  year={2026},
  publisher={GitHub},
  howpublished={\url{https://github.com/AstralVoidZ/ppsspp-dfx-mcp}},
}
```

## License

[MIT](LICENSE)
