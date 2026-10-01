"""Unit tests for evals/bridge.py — fake mode HTTP endpoints.

Run (from mcps/ppsspp-dfx-mcp/):
  <repo>/.venv/ppsspp-dfx-mcp/Scripts/python.exe -m pytest evals/test_bridge.py -q

W7 (review v3): the bridge is hardened — every request needs a bearer
token, POSTs need ``application/json`` + an acceptable ``Origin``, and
oversized bodies are rejected before they are read. The fixture injects a
known token via the ``BridgeServer(token=...)`` constructor seam instead
of scraping stdout.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from evals.bridge import BridgeCore, BridgeServer

_EVALS_DIR = Path(__file__).resolve().parent

# Injected token (the real entry point generates a random one and prints it).
_TOKEN = "test-bridge-token"


@pytest.fixture()
def bridge_url():
    core = BridgeCore(real=False, config_path=_EVALS_DIR / "config.yaml")
    server = BridgeServer(core, port=0, token=_TOKEN)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    # 90s budget (was 30s): macOS runners need more headroom for the stdio
    # MCP child to import + initialize. If the core start failed outright we
    # surface ITS exception instead of a bare timeout.
    deadline = time.time() + 90
    while time.time() < deadline and server._httpd is None and server.start_error is None:
        time.sleep(0.05)
    if server.start_error is not None:
        raise RuntimeError(f"bridge core failed to start: {server.start_error!r}")
    if server._httpd is None:
        raise RuntimeError("bridge failed to start within 90s")
    port = server._httpd.server_address[1]
    yield f"http://127.0.0.1:{port}"
    server._httpd.shutdown()
    t.join(timeout=10)


def _request(
    url: str,
    body: dict | None = None,
    *,
    token: str | None = _TOKEN,
    headers: dict[str, str] | None = None,
) -> urllib.request.Request:
    hdrs: dict[str, str] = {}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    if token is not None:
        hdrs["Authorization"] = f"Bearer {token}"
    if headers:
        hdrs.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    return urllib.request.Request(url, data=data, headers=hdrs)


def _post(url: str, body: dict, **kwargs: Any) -> dict:
    return json.loads(urllib.request.urlopen(_request(url, body, **kwargs), timeout=15).read())


def _rejected_code(url: str, body: dict | None = None, **kwargs: Any) -> int:
    """Return the HTTP status code of a request expected to be rejected."""
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(_request(url, body, **kwargs), timeout=15)
    return exc_info.value.code


def test_get_tools(bridge_url):
    resp = urllib.request.urlopen(_request(f"{bridge_url}/tools"), timeout=10)
    tools = json.loads(resp.read())
    assert isinstance(tools, list) and len(tools) > 0
    names = {t["name"] for t in tools}
    assert "ppsspp_health" in names
    assert "ppsspp_query" in names


def test_call_health(bridge_url):
    result = _post(f"{bridge_url}/call", {"tool": "ppsspp_health", "args": {}})
    assert result["is_error"] is False
    assert isinstance(result["text"], str) and len(result["text"]) > 0
    assert result["latency_ms"] >= 0


def test_call_unknown_tool(bridge_url):
    result = _post(f"{bridge_url}/call", {"tool": "no_such_tool", "args": {}})
    assert result["is_error"] is True


def test_call_missing_tool(bridge_url):
    assert _rejected_code(f"{bridge_url}/call", {"args": {}}) == 400


def test_seed_fake(bridge_url):
    result = _post(f"{bridge_url}/seed", {"iso_path": "fake_game.iso", "count": 1})
    assert "session_ids" in result
    assert len(result["session_ids"]) == 1


# ============================================================================
# W7 — auth / content-type / origin / size hardening
# ============================================================================


def test_get_tools_without_token_returns_401(bridge_url):
    assert _rejected_code(f"{bridge_url}/tools", token=None) == 401


def test_get_tools_wrong_token_returns_401(bridge_url):
    assert _rejected_code(f"{bridge_url}/tools", token="not-the-token") == 401


def test_post_without_token_returns_401(bridge_url):
    assert _rejected_code(f"{bridge_url}/call", {"tool": "ppsspp_health"}, token=None) == 401


def test_post_extra_token_header_accepted(bridge_url):
    """``X-Bridge-Token`` is an accepted alternative to the Bearer header."""
    result = _post(
        f"{bridge_url}/call",
        {"tool": "ppsspp_health", "args": {}},
        token=None,
        headers={"X-Bridge-Token": _TOKEN},
    )
    assert result["is_error"] is False


def test_wrong_content_type_returns_415(bridge_url):
    code = _rejected_code(
        f"{bridge_url}/call",
        {"tool": "ppsspp_health", "args": {}},
        headers={"Content-Type": "text/plain"},
    )
    assert code == 415


def test_oversized_content_length_returns_413(bridge_url):
    code = _rejected_code(
        f"{bridge_url}/call",
        {"tool": "ppsspp_health", "args": {}},
        headers={"Content-Length": str(2 * 1024 * 1024)},
    )
    assert code == 413


def test_evil_origin_returns_403(bridge_url):
    code = _rejected_code(
        f"{bridge_url}/call",
        {"tool": "ppsspp_health", "args": {}},
        headers={"Origin": "https://evil.example.com"},
    )
    assert code == 403


def test_loopback_origin_accepted(bridge_url):
    port = bridge_url.rsplit(":", 1)[1]
    result = _post(
        f"{bridge_url}/call",
        {"tool": "ppsspp_health", "args": {}},
        headers={"Origin": f"http://localhost:{port}"},
    )
    assert result["is_error"] is False
