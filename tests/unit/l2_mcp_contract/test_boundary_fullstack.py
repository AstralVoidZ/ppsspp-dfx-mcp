"""R9 (design_ppsspp_dfx_mcp_test_refactor_v2 §R9): full-stack boundary tests.

G3 lesson (verification report v2): the R7 boundary tests monkeypatched the
client layer, so the W1 double-clamp (tool 65536 → client 4096) passed 1199
unit tests and only surfaced in a real-MCP scenario. These tests keep the
FULL stack intact — real tool function → real PpssppDebugClient →
FakeTransport (only the WS socket is fake) — so a cross-layer constant
drift can no longer hide between layers.

Also pins the R9 single-source invariant: the memory limits live in
``tools/_common`` and every consumer imports them (a copied literal is the
bug pattern this file exists to prevent).
"""

from __future__ import annotations

import base64 as _b64
import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fake_transport import FakeTransport

from ppsspp_dfx_mcp.errors import ToolError
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient
from ppsspp_dfx_mcp.tools import _common
from ppsspp_dfx_mcp.tools import memory as memory_tool
from ppsspp_dfx_mcp.tools.memory import read_memory, write_memory


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()


@pytest.fixture
def real_client(fake_transport: FakeTransport) -> PpssppDebugClient:
    return PpssppDebugClient(fake_transport)


@pytest.fixture
def full_stack(
    fake_transport: FakeTransport,
    real_client: PpssppDebugClient,
    monkeypatch: pytest.MonkeyPatch,
):
    """Route the real tool through the REAL client over a FakeTransport.

    This is the no-hole wiring: only the WebSocket socket is faked; every
    layer between the tool function and the wire is production code.
    """

    @asynccontextmanager
    async def fake_session_client(
        session_id: str,
    ) -> AsyncIterator[PpssppDebugClient]:
        yield real_client

    monkeypatch.setattr("ppsspp_dfx_mcp.tools.memory.session_client", fake_session_client)
    return fake_transport


class TestFullStackStringCap:
    @pytest.mark.asyncio
    async def test_read_string_64k_through_every_layer(self, full_stack):
        """W1 regression, full-stack variant: max_len=65536 must survive the
        client layer intact. With the old client-side 4096 clamp this
        returns 4096 chars and fails — exactly the hole R7's mocked tests
        could not see."""
        payload = b"A" * 8000 + b"\x00" + b"padding"
        full_stack.set_response(
            "memory.read",
            {"base64": _b64.b64encode(payload).decode("ascii")},
        )
        result = await read_memory(
            session_id="sess-1",
            action="read_string",
            address="0x09FE0000",
            max_len=65536,
        )
        assert len(result["value"]) == 8000

    @pytest.mark.asyncio
    async def test_read_string_default_cap_4096_forwarded(self, full_stack):
        """The default (max_len<=0 → 4096) still reaches the wire intact."""
        full_stack.set_response(
            "memory.read", {"base64": _b64.b64encode(b"ok\x00").decode("ascii")}
        )
        await read_memory(session_id="sess-1", action="read_string", address="0x09FE0000")
        size = full_stack.calls[-1][1]["size"]
        assert size == _common.DEFAULT_STRING_CAP == 4096


# TestFullStackScanClamp removed — scan clamp covered by test_safety_guards_batch4


class TestWriteGuardOrder:
    @pytest.mark.asyncio
    async def test_protected_boundary_rejected_before_any_ws_traffic(self, full_stack):
        """W2, full-stack variant: the granularity-aware protection guard
        fires BEFORE any transport call (fail-fast, zero side effects)."""
        with pytest.raises(ToolError, match="PROTECTED_ADDRESS|protected"):
            await write_memory(
                session_id="sess-1",
                address="0x08803FFD",
                data="0x11223344",
                format="u32",
            )
        assert full_stack.calls == [], "the protection check must not touch the transport"


class TestConstantSingleSource:
    """R9 invariant: the memory limits exist ONCE, in tools/_common."""

    def test_memory_tool_reexports_are_identities(self):
        assert memory_tool._MAX_READ_BYTES is _common.MAX_SINGLE_READ_BYTES

    def test_no_stale_literal_in_client_read_string(self):
        src = inspect.getsource(PpssppDebugClient.read_string)
        # The DOCSTRING may legitimately mention 65536; the bug pattern is
        # the literal re-entering the CODE path (the old clamp expression).
        assert "min(max_length, 65536)" not in src, (
            "client read_string must clamp via MAX_SINGLE_READ_BYTES, not a "
            "copied literal (the W1 bug pattern)"
        )
        assert "MAX_SINGLE_READ_BYTES" in src

    def test_no_stale_literal_in_tool_read_memory(self):
        src = inspect.getsource(memory_tool.read_memory)
        assert "min(max_len, 65536)" not in src
        assert "MAX_SINGLE_READ_BYTES" in src
        assert "DEFAULT_STRING_CAP" in src


class TestFullStackReconnectHandshake:
    """R20 (design_ppsspp_dfx_mcp_test_refactor_v3 §G-12): W3 full-stack
    variant. The unit test (test_w3_reconnect_handshake.py) drives a bare
    WsTransport; this variant keeps the NO-HOLE stack intact — real tool
    function → real PpssppDebugClient → REAL WsTransport whose websockets
    socket is the only fake — and asserts the version handshake lands
    BEFORE the retried tool call on the re-established connection."""

    @pytest.mark.asyncio
    async def test_tool_call_after_drop_reconnects_with_version_first(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import asyncio
        import json as _json
        from unittest.mock import patch

        from websockets.protocol import State

        from ppsspp_dfx_mcp.core.transport import WsTransport

        class _EchoSocket:
            def __init__(self, ledger: list[list[str]]) -> None:
                self.subprotocol = "debugger.ppsspp.org"
                self.state = State.OPEN
                self.sent: list[str] = []
                ledger.append(self.sent)
                self._replies: asyncio.Queue[str] = asyncio.Queue()

            async def recv(self) -> str:
                return await self._replies.get()

            async def send(self, data: str) -> None:
                msg = _json.loads(data)
                self.sent.append(data)
                payload: dict[str, Any] = {"event": msg["event"], "ticket": msg["ticket"]}
                if msg["event"] == "memory.read_u32":
                    payload["value"] = 0x2A
                await self._replies.put(_json.dumps(payload))

            async def close(self) -> None:
                self.state = State.CLOSED

        ledger: list[list[str]] = []
        sockets = [_EchoSocket(ledger), _EchoSocket(ledger)]

        async def _fake_connect(*args: Any, **kwargs: Any) -> _EchoSocket:
            return sockets.pop(0)

        t = WsTransport("127.0.0.1", 12345)
        client = PpssppDebugClient(t)

        @asynccontextmanager
        async def fake_session_client(session_id: str):
            yield client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        with patch(
            "ppsspp_dfx_mcp.core.transport.websockets.connect",
            new=_fake_connect,
        ):
            await t.connect()
            # First call succeeds on the original connection.
            r1 = await read_memory(
                session_id="sess-1",
                action="read_u32",
                address="0x08804000",
            )
            assert r1["value"] == 42  # 0x2A

            # Simulate a mid-session drop, then call through the tool
            # again — the reconnect must re-handshake BEFORE serving it.
            sockets_running = ledger[0]
            t.ws.state = State.CLOSED

            r2 = await asyncio.wait_for(
                read_memory(
                    session_id="sess-1",
                    action="read_u32",
                    address="0x08804000",
                ),
                timeout=10.0,
            )
            assert r2["value"] == 42

        conn1_events = [_json.loads(m)["event"] for m in sockets_running]
        assert conn1_events == ["memory.read_u32"]
        conn2_events = [_json.loads(m)["event"] for m in ledger[1]]
        # W3 contract: version handshake FIRST on the new connection.
        assert conn2_events == ["version", "memory.read_u32"]
