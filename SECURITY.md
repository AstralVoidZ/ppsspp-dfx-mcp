# Security Policy

## Supported versions

This is a personal, alpha-stage project. Security fixes target the latest
commit on the default branch only.

## Reporting a vulnerability

Open a private security advisory via GitHub's
"Report a vulnerability" feature on the Security tab of this repository.
If that is unavailable, open a regular issue titled
`[security] <short summary>` and avoid including exploit details until
asked.

Please include:

- The affected file(s) / tool(s) and how you exercised them.
- A minimal reproduction (config, MCP client call, expected vs. actual).
- Impact assessment, especially for anything reachable beyond localhost.

## Scope notes

The server binds to loopback by default (`PPSSPP_DFX_WS_HOST=127.0.0.1`)
and launches a local emulator process; it is designed to run on a
developer workstation. The WebSocket connection to PPSSPP is
unauthenticated by upstream design — anyone who can reach the configured
host:port can drive the debugger. Do not expose `PPSSPP_DFX_WS_HOST`
beyond loopback without understanding this.

## Configuration trust boundary

The config tree (`.ppsspp-dfx/config/`) is treated as **trusted input**:
a writable config is equivalent to code execution on the workstation.
Two boundaries are enforced in code rather than assumed:

- **Script manifest** (`scripts.manifest.yaml`). Each entry's `path` may
  only name a `.py` file inside the project root — relative paths are
  containment-checked, so `..` cannot escape. Absolute paths bypass that
  check and are therefore **refused** unless the operator explicitly sets
  `PPSSPP_DFX_ALLOW_ABS_SCRIPT=1`. Set it only for trusted test fixtures or
  workspace-rewired dev scripts. Loading a manifest entry executes the file
  (`importlib`), so a manifest is as powerful as a shell script.
- **`project.yaml` → `ppsspp_exe`** (or `PPSSPP_DFX_EXE_PATH`). This is a
  trusted path by design: session startup launches that executable as a
  subprocess. Pointing it at an arbitrary binary runs that binary. Keep the
  config tree writable only by the account running the server, and prefer
  the env var when the config is shared.

`evals/bridge.py` is a developer-only eval helper (not part of the MCP
server surface). It binds to loopback and additionally requires a random
bearer token printed at startup (or `X-Bridge-Token`); POST bodies must be
`application/json`, at most 1 MiB, and carry no foreign `Origin`.

## Handling

- Acknowledgement within 7 days.
- Fixes prioritized over feature work; a patched commit is the deliverable
  (no SLA-guaranteed release cadence at this stage).
