"""ppsspp_session(action='start') must return a usable session.

Measured 2026-10-01 on a real PPSSPP (v1.20.4):

    session(start) -> ws_connected=false, ppsspp_version=null
    first call     -> [INTERNAL] version handshake timeout (5s)

and it stayed broken for 5 consecutive attempts over 137s. The same call
succeeded after the emulator had been given ~5s to warm up, which is what
`wait_ready=true` does.

Root cause: `tools/session.py` gates its readiness wait on
`wait_ready and test_mode() != "fake"`, and `wait_ready` defaults to
False. The documented "USAGE: start(...)" path therefore returns a
session whose transport has not completed the version handshake, so
every subsequent call re-handshakes and fails.

The contract being pinned here: after a successful `start`, a plain
`ppsspp_memory_map` MUST work without the caller doing anything else.
"""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp.errors import ArgsInvalid


class TestStartReturnsUsableSession:
    """The first call after start() must not need a warm-up."""

    @pytest.mark.asyncio
    async def test_start_defaults_are_documented_as_ready(self) -> None:
        """`wait_ready` must default to True.

        The tool docstring advertises start() as producing a ready
        session; with the default False it does not, and the caller has
        no signal that a second step is required.
        """
        import inspect

        from ppsspp_dfx_mcp.tools.session import session

        sig = inspect.signature(session)
        assert "wait_ready" in sig.parameters, "wait_ready parameter is gone"
        assert sig.parameters["wait_ready"].default is True, (
            "wait_ready must default to True: returning a session whose "
            "transport has not handshook makes every later call fail"
        )

    @pytest.mark.asyncio
    async def test_start_rejects_zero_probe_addr(self) -> None:
        """The zero-probe guard (S10) must survive the default change."""
        from ppsspp_dfx_mcp.tools.session import session

        with pytest.raises(ArgsInvalid, match="probe_addr"):
            await session(
                action="start",
                iso_path="dummy.iso",
                probe_addr="0x0",
            )


class TestHandshakeBudget:
    """The handshake budget must tolerate a slow start.

    send_version() splits its per-instance budget between the ticket path
    (``min(2.0, budget / 2)``) and the fallback poll of the dedicated version
    queue (W30). Under load the debugger needs longer, and the pre-D22 error
    text ("timeout (5s)") misreported the real budget.
    """

    def test_send_version_documents_its_budget(self) -> None:
        import inspect

        from ppsspp_dfx_mcp.core.transport import WsTransport

        doc = inspect.getdoc(WsTransport.send_version) or ""
        assert "timeout" in doc.lower()

    @pytest.mark.asyncio
    async def test_transport_exposes_a_configurable_handshake_budget(self) -> None:
        """A slow start must be fixable without editing the transport."""
        from ppsspp_dfx_mcp.core.transport import WsTransport

        t = WsTransport("127.0.0.1", 1)
        assert hasattr(t, "handshake_timeout_s"), (
            "handshake budget must be configurable; a hard-coded 5s makes slow starts unrecoverable"
        )
        assert t.handshake_timeout_s > 0
