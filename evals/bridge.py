"""MCP HTTP bridge for subagent-driven eval collection.

A long-lived stdlib HTTP server (127.0.0.1 only) that holds a single
long-lived mcp stdio ClientSession to ppsspp-dfx-mcp. Subagents call it
with plain curl instead of speaking MCP JSON-RPC, and the bridge injects
evals-specific logic (pre_state seed + real settle wait) that the MCP
protocol layer has no place for.

Usage (from mcps/ppsspp-dfx-mcp/):
  <venv python> -m evals.bridge --port 8765           # fake mode
  <venv python> -m evals.bridge --port 8765 --real    # real PPSSPP (needs EXE/ISO env)

The server prints a random bearer token at startup (W7, review v3); every
request must present it, e.g.
  curl -H "Authorization: Bearer <token>" http://127.0.0.1:8765/tools
POST bodies must be application/json, ≤1 MiB, and carry no foreign Origin.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_EVALS_DIR = Path(__file__).resolve().parent
_PKG_ROOT = _EVALS_DIR.parent
_SRC_ROOT = _PKG_ROOT / "src"
_TESTS_ROOT = _PKG_ROOT / "tests"

_CALL_TIMEOUT_S = 300

# W7 (review v3): request guards. The bridge can drive an emulator
# (memory read/write, input injection), so loopback binding alone is not a
# trust boundary — any local process, and any web page that can reach
# 127.0.0.1, would otherwise be able to call it.
_MAX_BODY_BYTES = 1 << 20  # 1 MiB


def _result_text(result: Any) -> str:
    """Flatten a tool result into text (three channels, same as runner.py)."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        elif getattr(block, "data", None) is not None:
            mime = getattr(block, "mime_type", "image")
            raw = str(getattr(block, "data", ""))
            parts.append(f"[ImageContent {mime}, {len(raw)} base64 chars]")
    sc = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if sc:
        parts.append(json.dumps(sc, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


def _structured(result: Any) -> Any:
    return getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)


class BridgeCore:
    """Async core owning the long-lived mcp stdio ClientSession."""

    def __init__(self, real: bool, config_path: Path) -> None:
        self.real = real
        self.config_path = config_path
        self.session: ClientSession | None = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._sessions_dir: Path | None = None
        self._real_settle_s: float = 8.0

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.real:
            env.pop("PPSSPP_DFX_TEST_MODE", None)
            env.pop("PPSSPP_DFX_FIXTURE_DIR", None)
            env["PPSSPP_DFX_EXE_PATH"] = os.environ.get(
                "PPSSPP_DFX_TEST_EXE_PATH", "PPSSPPWindows64.exe"
            )
        else:
            scen_cfg = yaml.safe_load((_EVALS_DIR / "scenarios.yaml").read_text(encoding="utf-8"))
            fixtures_dir = (_EVALS_DIR / scen_cfg["fixtures_dir"]).resolve()
            env["PPSSPP_DFX_TEST_MODE"] = "fake"
            env["PPSSPP_DFX_FIXTURE_DIR"] = str(fixtures_dir)
        env["PPSSPP_DFX_LOG_LEVEL"] = "WARNING"
        env["PPSSPP_DFX_SESSIONS_PATH"] = str(self._sessions_dir / "sessions.json")
        env["PYTHONPATH"] = os.pathsep.join(
            [str(_SRC_ROOT), str(_TESTS_ROOT), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        return env

    async def start(self) -> None:
        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self._real_settle_s = float(cfg.get("limits", {}).get("real_settle_s", 8))
        self._sessions_dir = Path(tempfile.mkdtemp(prefix="ppsspp-dfx-bridge-sessions-"))
        self._stack = contextlib.AsyncExitStack()
        env = self._build_env()
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "ppsspp_dfx_mcp"], env=env
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()

    async def stop(self) -> None:
        if self._stack:
            await self._stack.aclose()

    async def list_tools(self) -> list[dict[str, Any]]:
        tools = (await self.session.list_tools()).tools
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": getattr(t, "input_schema", None)
                or getattr(t, "inputSchema", None)
                or {"type": "object", "properties": {}},
            }
            for t in tools
        ]

    async def call_tool(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        t0 = time.monotonic()
        result = await self.session.call_tool(tool, args)
        is_error = bool(getattr(result, "is_error", getattr(result, "isError", False)))
        latency_ms = int((time.monotonic() - t0) * 1000)
        return {
            "is_error": is_error,
            "text": _result_text(result),
            "structured_content": _structured(result),
            "latency_ms": latency_ms,
        }

    async def seed(self, iso_path: str, count: int) -> list[str]:
        ids: list[str] = []
        for _ in range(max(0, count)):
            result = await self.session.call_tool(
                "ppsspp_session",
                {"action": "start", "iso_path": iso_path, "wait_ready": True, "resilient": True},
            )
            if getattr(result, "is_error", getattr(result, "isError", False)):
                raise RuntimeError(f"session start failed: {_result_text(result)[:300]}")
            sid = (_structured(result) or {}).get("session_id")
            if not sid:
                try:
                    sid = json.loads(_result_text(result)).get("session_id")
                except (json.JSONDecodeError, AttributeError):
                    sid = None
            if not sid:
                raise RuntimeError(f"no session_id: {_result_text(result)[:300]}")
            ids.append(str(sid))
        if self.real and ids:
            await asyncio.sleep(self._real_settle_s)
        return ids


class BridgeServer:
    """Sync HTTP facade over BridgeCore (runs core on a background loop)."""

    def __init__(self, core: BridgeCore, port: int, token: str | None = None) -> None:
        self.core = core
        self.port = port
        # W7 (review v3): per-process bearer token, printed by serve() so a
        # human (and the eval runner) can use it. The constructor seam
        # (token=None → random) exists so tests can inject a known value.
        self.token: str = token if token is not None else secrets.token_urlsafe(32)
        self.loop = asyncio.new_event_loop()
        self._loop_thread: threading.Thread | None = None
        self._httpd: ThreadingHTTPServer | None = None

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro: Any) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=_CALL_TIMEOUT_S)

    def serve(self) -> None:
        self._loop_thread = threading.Thread(target=self._run_loop, daemon=True)
        self._loop_thread.start()
        self.submit(self.core.start())
        self._httpd = ThreadingHTTPServer(("127.0.0.1", self.port), self._make_handler())
        print(
            f"bridge on http://127.0.0.1:{self.port} (real={self.core.real})\n"
            f"bridge token: {self.token}\n"
            f'  send it as: -H "Authorization: Bearer {self.token}" '
            f'(or -H "X-Bridge-Token: {self.token}")',
            flush=True,
        )
        try:
            self._httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            with contextlib.suppress(Exception):
                self.submit(self.core.stop())
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._httpd.server_close()

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        core = self.core
        submit = self.submit
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body: Any) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            # ── W7 guards ───────────────────────────────────────────────

            def _authorized(self) -> bool:
                """Bearer token (or X-Bridge-Token) must match the process token."""
                header = self.headers.get("Authorization", "")
                if header.lower().startswith("bearer "):
                    presented = header[len("Bearer ") :].strip()
                else:
                    presented = self.headers.get("X-Bridge-Token", "").strip()
                if not presented:
                    return False
                # Bytes compare: constant-time AND crash-free for non-ASCII
                # header junk (str compare_digest raises on non-ASCII).
                return secrets.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))

            def _origin_allowed(self) -> bool:
                """Reject cross-origin browser requests (CSRF-lite).

                Absent Origin = non-browser client (curl / eval runner) →
                allowed. A present Origin must be this bridge's own
                loopback origin.
                """
                origin = self.headers.get("Origin")
                if not origin:
                    return True
                port = self.server.server_address[1]  # type: ignore[attr-defined]
                return origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

            def _require_authorized(self) -> bool:
                if self._authorized():
                    return True
                self._send(401, {"error": "unauthorized: missing or invalid bridge token"})
                return False

            def _require_allowed_origin(self) -> bool:
                if self._origin_allowed():
                    return True
                self._send(403, {"error": f"forbidden origin: {self.headers.get('Origin')}"})
                return False

            def _require_json_content_type(self) -> bool:
                ctype = self.headers.get("Content-Type", "")
                if ctype.split(";", 1)[0].strip().lower() == "application/json":
                    return True
                self._send(
                    415,
                    {
                        "error": f"unsupported content-type: {ctype or '(missing)'}; "
                        "expected application/json"
                    },
                )
                return False

            def _read_body(self) -> bytes | None:
                """Read the declared body, rejecting anything over the cap.

                The size check happens BEFORE the read: a hostile or
                mistaken Content-Length would otherwise park this handler
                reading bytes that never arrive.
                """
                try:
                    declared = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    self._send(400, {"error": "invalid Content-Length"})
                    return None
                if declared > _MAX_BODY_BYTES:
                    self._send(
                        413,
                        {"error": f"body too large: {declared} bytes (limit {_MAX_BODY_BYTES})"},
                    )
                    return None
                if declared <= 0:
                    return b"{}"
                return self.rfile.read(min(declared, _MAX_BODY_BYTES))

            # ── Routes ──────────────────────────────────────────────────

            def do_GET(self) -> None:
                if not self._require_authorized() or not self._require_allowed_origin():
                    return
                if self.path == "/tools":
                    try:
                        self._send(200, submit(core.list_tools()))
                    except Exception as exc:
                        self._send(500, {"error": str(exc)})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                if (
                    not self._require_authorized()
                    or not self._require_allowed_origin()
                    or not self._require_json_content_type()
                ):
                    return
                raw = self._read_body()
                if raw is None:
                    return
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError as exc:
                    self._send(400, {"error": f"invalid json: {exc}"})
                    return
                if self.path == "/call":
                    self._handle_call(body)
                elif self.path == "/seed":
                    self._handle_seed(body)
                else:
                    self._send(404, {"error": "not found"})

            def _handle_call(self, body: dict[str, Any]) -> None:
                tool = body.get("tool")
                if not tool:
                    self._send(400, {"error": "missing tool"})
                    return
                args = body.get("args") or {}
                sid = body.get("session_id")
                if sid and "session_id" not in args:
                    args["session_id"] = sid
                try:
                    self._send(200, submit(core.call_tool(tool, args)))
                except Exception as exc:
                    self._send(500, {"error": str(exc)})

            def _handle_seed(self, body: dict[str, Any]) -> None:
                iso_path = body.get("iso_path")
                if not iso_path:
                    self._send(400, {"error": "missing iso_path"})
                    return
                count = int(body.get("count", 1))
                try:
                    ids = submit(core.seed(iso_path, count))
                    self._send(200, {"session_ids": ids})
                except Exception as exc:
                    self._send(500, {"error": str(exc)})

            def log_message(self, fmt: str, *a: Any) -> None:
                pass

        return Handler


def main() -> None:
    p = argparse.ArgumentParser(description="ppsspp-dfx MCP HTTP bridge for subagent collection")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--real", action="store_true", help="real PPSSPP mode (needs EXE/ISO env)")
    p.add_argument("--config", default=str(_EVALS_DIR / "config.yaml"))
    args = p.parse_args()
    core = BridgeCore(args.real, Path(args.config))
    BridgeServer(core, args.port).serve()


if __name__ == "__main__":
    main()
