"""Service package — domain-specific client composition layer.

Sits above `core/` (transport, stepping) and below `tools/`:
- `debug_client.PpssppDebugClient` — composes WsTransport + SteppingManager,
  exposes ~50 domain methods organized by category (Memory, CPU, Stepping,
  Breakpoint, HLE, Disasm, Input, System, GPU Buffer). All WS event names
  are internal to this class.
- `capture.CaptureService` — composes PpssppDebugClient (NOT WsTransport),
  provides screenshot/texture/buffer capture with strategy chain fallback
  and Win32 GDI interop.
"""
