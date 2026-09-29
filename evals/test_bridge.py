"""Unit tests for evals/bridge.py — fake mode HTTP endpoints.

Run (from mcps/ppsspp-dfx-mcp/):
  .venv-test/Scripts/python.exe -m pytest evals/test_bridge.py -q
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from evals.bridge import BridgeCore, BridgeServer

_EVALS_DIR = Path(__file__).resolve().parent


@pytest.fixture()
def bridge_url():
    core = BridgeCore(real=False, config_path=_EVALS_DIR / "config.yaml")
    server = BridgeServer(core, port=0)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    deadline = time.time() + 30
    while time.time() < deadline and server._httpd is None:
        time.sleep(0.05)
    if server._httpd is None:
        raise RuntimeError("bridge failed to start within 30s")
    port = server._httpd.server_address[1]
    yield f"http://127.0.0.1:{port}"
    server._httpd.shutdown()
    t.join(timeout=10)


def _post(url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def test_get_tools(bridge_url):
    resp = urllib.request.urlopen(f"{bridge_url}/tools", timeout=10)
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
    req = urllib.request.Request(
        f"{bridge_url}/call",
        data=json.dumps({"args": {}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req, timeout=10)
    assert exc_info.value.code == 400


def test_seed_fake(bridge_url):
    result = _post(f"{bridge_url}/seed", {"iso_path": "fake_game.iso", "count": 1})
    assert "session_ids" in result
    assert len(result["session_ids"]) == 1