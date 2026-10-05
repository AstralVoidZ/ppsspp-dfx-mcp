"""test_ws_upgrade_no_subprotocol.py — W-1: handshake-failure residue (specs/010 US3).

FR-017 / C5-6 / M-19: the WS handshake-failure path must leave NO connection
object behind and leak no file descriptors, proven by repeated failure.

Two tiers (conclusion levels, per FR-017):
- LOCAL STAND-IN (no device): WsTransport against a local server that does
  not negotiate any subprotocol — the exact branch in ``connect()``
  (transport.py: subprotocol mismatch -> close + ``ws = None`` + RuntimeError).
  Runs everywhere; no ``real_ppsspp`` gate. Conclusion level for this tier:
  ``替身等价`` (stand-in equivalent).
- REAL DEVICE (``real_ppsspp`` gated): against live PPSSPP, an upgrade
  WITHOUT our subprotocol must fail the negotiation (rejected outright, or
  accepted with a subprotocol != ``debugger.ppsspp.org`` — both are
  handshake failures for our protocol), and repeating the failure must not
  grow the process handle/FD count. Conclusion level when run: ``实测``.

FD counting is dual-platform without new dependencies:
- Windows: kernel32.GetProcessHandleCount (handle count of this process)
- POSIX: len(os.listdir('/proc/self/fd'))

A tolerance of 2 is allowed: the pytest process itself allocates unrelated
handles (logging, tmp files) between samples; a per-attempt socket leak
(1 fd x 15 attempts = 15) cannot hide under that tolerance.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os

import pytest
import websockets
from websockets.typing import Subprotocol

from ppsspp_dfx_mcp.core.transport import WS_SUBPROTOCOL, WsTransport

_FD_PROBES = 15  # repeated-failure count for the leak measurement
_FD_TOLERANCE = 2


def _fd_count() -> int:
    """Open-handle/FD count of THIS process (Windows + POSIX, no deps)."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # Explicit prototypes: the default int-based marshalling mangles the
        # pseudo-handle from GetCurrentProcess and the call fails.
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel32.GetProcessHandleCount.restype = ctypes.c_int
        handle = kernel32.GetCurrentProcess()
        count = ctypes.c_ulong(0)
        if not kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
            raise OSError("GetProcessHandleCount failed")
        return int(count.value)
    # /proc/self/fd is Linux-only; macOS exposes the same table at /dev/fd.
    # Raising (instead of skipping) keeps the leak check honest: a silent
    # skip would report green while measuring nothing.
    for probe in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(probe):
            return len(os.listdir(probe))
    raise OSError("no fd table available at /proc/self/fd or /dev/fd")


# ── Local stand-in: WsTransport vs a server that selects no subprotocol ──


async def _no_subprotocol_server(port: int = 0):
    """Serve WS without negotiating any subprotocol (legacy serve default)."""
    return await websockets.serve(lambda ws: ws.wait_closed(), "127.0.0.1", port)


class TestLocalSubprotocolNegotiation:
    """替身等价 tier: the mismatch branch in WsTransport.connect() itself."""

    async def test_connect_raises_and_clears_the_connection_object(self):
        server = await _no_subprotocol_server()
        port = server.sockets[0].getsockname()[1]
        try:
            transport = WsTransport("127.0.0.1", port)
            with pytest.raises(RuntimeError, match="subprotocol"):
                await transport.connect()
            # The socket must NOT stay assigned: an OPEN socket here would
            # make is_connected() True and short-circuit _ensure_connected().
            assert transport.ws is None, (
                "subprotocol mismatch left a connection object behind — "
                "later calls would burn their timeout on an undrained socket"
            )
            assert transport.is_connected() is False
        finally:
            server.close()
            await server.wait_closed()

    async def test_repeated_failures_leak_no_file_descriptors(self):
        server = await _no_subprotocol_server()
        port = server.sockets[0].getsockname()[1]
        try:
            gc.collect()
            before = _fd_count()
            for _ in range(_FD_PROBES):
                transport = WsTransport("127.0.0.1", port)
                with pytest.raises(RuntimeError, match="subprotocol"):
                    await transport.connect()
                assert transport.ws is None
            gc.collect()
            after = _fd_count()
            assert after - before <= _FD_TOLERANCE, (
                f"{_FD_PROBES} handshake failures grew the FD/handle count "
                f"by {after - before} (> tolerance {_FD_TOLERANCE}) — socket leak"
            )
        finally:
            server.close()
            await server.wait_closed()


# ── Real device: live PPSSPP rejection behaviour + residue (W-1) ──────────


@pytest.mark.real_ppsspp
class TestRealDeviceHandshakeResidue:
    """实测 tier (needs PPSSPP_DFX_TEST_EXE_PATH/_ISO_PATH)."""

    async def test_upgrade_without_our_subprotocol_fails(self, real_ppsspp_ws_url: str):
        """An upgrade WITHOUT subprotocols must not yield a usable connection.

        Either PPSSPP rejects the upgrade outright (handshake error) or it
        accepts with no/other subprotocol — both are handshake failures for
        our protocol (the server's debugger endpoint is subprotocol-gated).
        """
        conn = None
        try:
            conn = await websockets.connect(real_ppsspp_ws_url, open_timeout=5)
            # Accepted the upgrade but did not select our subprotocol:
            assert conn.subprotocol != Subprotocol(WS_SUBPROTOCOL), (
                "PPSSPP negotiated debugger.ppsspp.org for an upgrade that "
                "did not request it — the subprotocol gate is not effective"
            )
        except Exception as exc:  # noqa: BLE001 — rejected outright is fine
            assert not isinstance(exc, asyncio.TimeoutError), (
                "upgrade without subprotocol timed out instead of being rejected"
            )
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()

    async def test_repeated_failures_leak_no_file_descriptors(self, real_ppsspp_ws_url: str):
        """Repeating the failed handshake must not grow the handle count."""
        gc.collect()
        before = _fd_count()
        for _ in range(_FD_PROBES):
            conn = None
            try:
                conn = await websockets.connect(real_ppsspp_ws_url, open_timeout=5)
                assert conn.subprotocol != Subprotocol(WS_SUBPROTOCOL)
            except Exception:  # noqa: BLE001 — outright rejection is fine
                conn = None
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close()
        gc.collect()
        after = _fd_count()
        assert after - before <= _FD_TOLERANCE, (
            f"{_FD_PROBES} failed handshakes grew the FD/handle count by "
            f"{after - before} (> tolerance {_FD_TOLERANCE}) — connection residue leak"
        )

    async def test_wstransport_connects_cleanly_after_failed_upgrades(
        self, real_ppsspp_ws_url: str
    ):
        """The failure path must not poison a subsequent legitimate connect.

        W-1's operational concern: residue from failed upgrades (half-open
        sockets on the PPSSPP side or client-side FDs) must not degrade the
        next real connection.
        """
        for _ in range(3):
            conn = None
            try:
                conn = await websockets.connect(real_ppsspp_ws_url, open_timeout=5)
            except Exception:  # noqa: BLE001
                conn = None
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        await conn.close()

        transport = WsTransport(
            "127.0.0.1", int(real_ppsspp_ws_url.rsplit(":", 1)[1].split("/")[0])
        )
        await transport.connect()
        try:
            assert transport.is_connected() is True
        finally:
            with contextlib.suppress(Exception):
                await transport.close()
