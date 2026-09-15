"""Session package — session-scoped helpers and lifecycle management.

Renamed per ppsspp-dfx-architecture-refactor (task 6.1); the legacy
orchestration package was split into the current session/ layout. Holds:
- `session_manager` — session lifecycle (start/stop/get/list)
- `client_helper` — `session_client` / `session_capture` context managers
  yielding PpssppDebugClient and (DebugClient, CaptureService) tuples,
  plus async helper (`validate_session_alive`) and sync helper (`read_game_mode_addr`).

Layer 9 cleanup removed the legacy thin pass-through wrappers
(breakpoint_handler / safe_query / screenshot_orchestrator) — tools
now call PpssppDebugClient / CaptureService methods directly via
`session_client` / `session_capture`.
"""
