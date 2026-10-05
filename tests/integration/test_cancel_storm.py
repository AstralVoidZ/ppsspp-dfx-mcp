"""test_cancel_storm.py — W-7: concurrent-cancellation storm (specs/010 US3).

FR-020 / C5-8: session cancellation under concurrent cancellation MUST have a
re-runnable verification; no cancellation may be swallowed and the background
task set must converge.

Runs fully local (no real device): a local slow-echo WS server stands in for
PPSSPP (conclusion level ``替身等价`` for the transport layer; the real-device
half of W-7 is covered by the session-lifecycle tests under the real-device
gate). The storm exercises the exact cancellation contract implemented in
``transport.call()``:

- client-timeout cancellation (``asyncio.wait_for`` inside ``call`` pops the
  pending ticket and re-raises TimeoutError),
- external task cancellation (the ``except BaseException`` branch pops the
  pending ticket and RE-RAISES — a swallowed CancelledError would surface as
  ``task.cancelled() is False``),
- post-storm recovery (a normal call must still succeed — the storm must not
  tear the transport), and
- task convergence (no leaked pending-ticket futures, no orphaned tasks
  beyond the transport's own recv loop; orphaned coroutines additionally fail
  the suite via the global ``error::RuntimeWarning`` filter).
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json

import websockets

from ppsspp_dfx_mcp.core.transport import WsTransport

_ECHO_DELAY_S = 0.3
_STORM_SIZE = 32
_CANCEL_SIZE = 8
_CANCEL_DELAY_S = 0.05


async def _slow_echo(ws) -> None:
    """Reply to every request after a delay, echoing the ticket.

    Each request is processed CONCURRENTLY (like PPSSPP, which answers
    requests from its receive loop without head-of-line blocking): a storm
    of 40 requests must not make the server serialise 12s of backlog —
    the storm's client-side cancellations are what we verify, not server
    queueing.
    """

    async def _answer(raw: str) -> None:
        msg = json.loads(raw)
        await asyncio.sleep(_ECHO_DELAY_S)
        with contextlib.suppress(Exception):  # client may be gone by then
            await ws.send(
                json.dumps({"event": msg["event"], "ticket": msg["ticket"], "echo": True})
            )

    async for raw in ws:
        asyncio.create_task(_answer(raw))


async def _start_echo_server():
    # The server MUST negotiate the transport's subprotocol (like PPSSPP does)
    # or WsTransport.connect() fails the handshake before the storm can start.
    server = await websockets.serve(
        _slow_echo, "127.0.0.1", 0, subprotocols=["debugger.ppsspp.org"]
    )
    port = server.sockets[0].getsockname()[1]
    return server, port


def _connected_tasks() -> set[asyncio.Task]:
    """All pending tasks except the one running the test."""
    return {t for t in asyncio.all_tasks() if t is not asyncio.current_task()}


def _is_ws_library_owned(task: asyncio.Task) -> bool:
    """True for websockets-library internals and the in-process echo server.

    The storm's convergence contract covers tasks OUR code creates (call
    tasks, recv loop, probes). websockets' per-connection tasks
    (transfer_data / keepalive_ping / close_connection) and the in-process
    echo server's handler/accept tasks are lifecycle-managed by the library
    and live until close() — they are not our leak surface. (Measured:
    the stray set after both storms is exclusively these, with the
    connection object unique throughout — no connection leak.)
    """
    qualname = getattr(task.get_coro(), "__qualname__", "")
    return (
        "WebSocketCommonProtocol" in qualname
        or "WebSocketServerProtocol" in qualname
        or qualname.endswith("_slow_echo")
        or "accept_coro" in qualname
    )


class TestCancelStorm:
    async def test_timeout_storm_pops_every_pending_ticket(self):
        """32 concurrent calls that all time out must strand no ticket."""
        server, port = await _start_echo_server()
        try:
            transport = WsTransport("127.0.0.1", port)
            await transport.connect()
            try:
                results = await asyncio.gather(
                    *(
                        asyncio.wait_for(
                            transport.call("echo", timeout=_CANCEL_DELAY_S), timeout=10
                        )
                        for _ in range(_STORM_SIZE)
                    ),
                    return_exceptions=True,
                )
                timeouts = [r for r in results if isinstance(r, TimeoutError)]
                assert len(timeouts) == _STORM_SIZE, (
                    f"expected {_STORM_SIZE} TimeoutError, got "
                    f"{len(timeouts)}; other outcomes: "
                    f"{[type(r).__name__ for r in results if not isinstance(r, TimeoutError)]}"
                )
                assert not transport._pending, (
                    f"timeout storm stranded {len(transport._pending)} pending tickets — "
                    "late responses would hit cancelled futures"
                )
            finally:
                with contextlib.suppress(Exception):
                    await transport.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_external_cancellation_is_not_swallowed(self):
        """Externally cancelled calls must end CANCELLED (re-raised, not eaten).

        The contract in ``call()`` is ``except BaseException: pop + raise``.
        A swallowed CancelledError would make ``task.cancelled()`` False.
        """
        server, port = await _start_echo_server()
        try:
            transport = WsTransport("127.0.0.1", port)
            await transport.connect()
            try:
                tasks = [
                    asyncio.create_task(transport.call("echo", timeout=10))
                    for _ in range(_CANCEL_SIZE)
                ]
                await asyncio.sleep(_CANCEL_DELAY_S)
                for t in tasks:
                    t.cancel()
                results = await asyncio.gather(*tasks, return_exceptions=True)
                cancelled = [r for r in results if isinstance(r, asyncio.CancelledError)]
                assert len(cancelled) == _CANCEL_SIZE, (
                    "cancellation was swallowed: outcomes were "
                    f"{[type(r).__name__ for r in results]}"
                )
                assert all(t.cancelled() for t in tasks), (
                    "tasks did not end in the CANCELLED state — "
                    "someone caught CancelledError without re-raising"
                )
                assert not transport._pending, (
                    f"cancel storm stranded {len(transport._pending)} pending tickets"
                )
            finally:
                with contextlib.suppress(Exception):
                    await transport.close()
        finally:
            server.close()
            await server.wait_closed()

    async def test_storm_does_not_tear_the_transport_and_tasks_converge(self):
        """After both storms, one normal call succeeds and the task set converges."""
        server, port = await _start_echo_server()
        try:
            transport = WsTransport("127.0.0.1", port)
            await transport.connect()
            try:
                # Storm 1: timeouts.
                await asyncio.gather(
                    *(
                        asyncio.wait_for(
                            transport.call("echo", timeout=_CANCEL_DELAY_S), timeout=10
                        )
                        for _ in range(_STORM_SIZE)
                    ),
                    return_exceptions=True,
                )
                # Storm 2: external cancellation.
                tasks = [
                    asyncio.create_task(transport.call("echo", timeout=10))
                    for _ in range(_CANCEL_SIZE)
                ]
                await asyncio.sleep(_CANCEL_DELAY_S)
                for t in tasks:
                    t.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.gather(*tasks, return_exceptions=True)

                # Recovery: the transport must still serve a normal call.
                resp = await transport.call("echo", timeout=5)
                assert resp.get("echo") is True, f"post-storm call broken: {resp!r}"

                # Convergence: only the recv loop (transport-owned) and
                # websockets-library internals may remain.
                await asyncio.sleep(0.2)
                gc.collect()
                recv = transport._recv_task
                strays = [
                    t
                    for t in _connected_tasks()
                    if t is not recv and not t.done() and not _is_ws_library_owned(t)
                ]
                assert not strays, (
                    f"task set did not converge: {[(t.get_name(), type(t)) for t in strays]}"
                )
                assert not transport._pending, "pending tickets leaked across the storm"
            finally:
                with contextlib.suppress(Exception):
                    await transport.close()
        finally:
            server.close()
            await server.wait_closed()
