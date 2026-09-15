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

## Handling

- Acknowledgement within 7 days.
- Fixes prioritized over feature work; a patched commit is the deliverable
  (no SLA-guaranteed release cadence at this stage).
