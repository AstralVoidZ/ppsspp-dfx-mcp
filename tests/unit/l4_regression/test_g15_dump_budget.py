"""L4 regression tests for G-15 (T070): dump budget bounds the whole capture.

Deep-test finding (``mcp_test_report/tools/ppsspp_dump.md``): an EMPTY
``ppsspp_dump`` spent 22.06s (texture) / 31.74s (clut) in the wedged run
``mcp_test_report/logs/_run5_fullseq_stuck``. Server stderr evidence shows
four stacked fallback budgets, not one constant:
- with_stepping preamble (entry probe 5s + pause probe 1s + pause
  confirmation 3.5s) ~= 10s;
- the texture call's own 15s timeout (+0.5s probe) plus a failed 3s
  resume after the body raised ~= 22s (P3-36: 22.06s);
- a transport reconnect (connect + 15s version handshake) burned
  another ~22s BEFORE the capture started (P3-37: 31.74s).
The reconnect path lives inside ``transport.call()``, so no per-event
timeout can bound it — only one budget around the whole operation can.

Fix under test: ``_DUMP_TOTAL_BUDGET_S`` (3.0s) wraps preamble + GPU
call + resume in both ``dump_texture()`` and ``dump_clut()``. Healthy
probes (2026-10-05, real stdio client, v1.20.4): EMPTY capture
p50=20.6ms / p95=25.8ms (n=20, title screen) and p50=33.3ms /
max=37.7ms (n=16) — the budget keeps a ~80x margin.

Test groups:
- G-15a (value): the budget is a measured-short ceiling far below the
  MCP client's 30s read timeout (a revert to a 5-15s value turns red).
- G-15b (bounded): a wedged link (never answers) returns b"" within
  the budget — for a hang in the GPU capture AND for a hang in the
  stepping preamble. Without the budget these tests hang and
  ``asyncio.wait_for`` turns them red.
- G-15c (non-regression): successful captures and non-timeout failures
  keep their existing contracts (PNG bytes / b"").
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Any

import pytest
from fake_transport import FakeTransport

from ppsspp_dfx_mcp.service import capture as capture_module
from ppsspp_dfx_mcp.service.capture import _DUMP_TOTAL_BUDGET_S, CaptureService
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

# Shrunken budget used by the wedged-link tests: the assertions measure
# the WIRING (the budget ends the wait), not the production wall-clock
# value (already pinned by the G-15a value test).
_SMALL_BUDGET_S = 0.2

# wait_for ceiling: without the budget the wedged call never returns, so
# this converts a missing-budget regression into a test failure instead
# of a hang. Deliberately well below the old 22-32s fallback stack.
_HANG_CEILING_S = 1.0


def _make_data_uri(png_bytes: bytes) -> str:
    return f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"


class _WedgedTransport(FakeTransport):
    """FakeTransport whose link stops answering one event (wedged debugger).

    Models the deep-test wedged run: the request reaches the transport
    but no response ever arrives (the real ``WsTransport`` reconnect path
    is similarly unbounded from the caller's perspective). The only thing
    that can end the await is the dump's own total budget.
    """

    def __init__(self, hang_event: str) -> None:
        super().__init__()
        self._hang_event = hang_event

    async def call(self, event: str, timeout: float = 5.0, **params: Any) -> dict[str, Any]:
        if event == self._hang_event:
            self.calls.append((event, dict(params)))
            await asyncio.Event().wait()  # never set — only cancellation ends this
        return await super().call(event, timeout=timeout, **params)


def _make_wedged_service(hang_event: str) -> tuple[CaptureService, _WedgedTransport]:
    """CaptureService backed by a transport that never answers ``hang_event``."""
    transport = _WedgedTransport(hang_event)
    transport.set_state({"stepping": False})
    transport.set_faf_handler("cpu.stepping", lambda t, **_: t.set_state({"stepping": True}))
    transport.set_faf_handler("cpu.resume", lambda t, **_: t.set_state({"stepping": False}))
    client = PpssppDebugClient(transport)
    return CaptureService(client, transport=transport), transport


def _faf_events(transport: FakeTransport) -> list[str]:
    return [ev for ev, _ in transport.fire_and_forget_calls]


class TestG15aBudgetValue:
    """G-15a: the budget is a measured-short ceiling (falsifiable)."""

    def test_budget_is_measured_short_and_far_below_read_timeout(self) -> None:
        """The budget must stay <= 3.0s — a 5-15s value turns this red.

        Falsifiability: setting ``_DUMP_TOTAL_BUDGET_S`` back to the old
        per-layer scale (5s entry probe / 15s texture timeout) fails
        here. 3.0s keeps a ~80x margin over the slowest healthy round
        trip measured on 2026-10-05 (37.7ms) and stays an order of
        magnitude below the MCP client's 30s read timeout (G-15: 22-32s
        responses in the wedged run).
        """
        assert 0 < _DUMP_TOTAL_BUDGET_S <= 3.0, (
            f"_DUMP_TOTAL_BUDGET_S={_DUMP_TOTAL_BUDGET_S} — the dump budget "
            "must bound the whole operation (stepping preamble + GPU call "
            "+ resume) well below the MCP client's 30s read timeout; the "
            "wedged run spent 22-32s because four fallback budgets stacked "
            "(G-15)."
        )


class TestG15bWedgedLinkIsBounded:
    """G-15b: a wedged link answers b"" within the budget."""

    @pytest.fixture
    def small_budget(self, monkeypatch: pytest.MonkeyPatch) -> float:
        monkeypatch.setattr(capture_module, "_DUMP_TOTAL_BUDGET_S", _SMALL_BUDGET_S)
        return _SMALL_BUDGET_S

    async def test_wedged_texture_capture_returns_empty_within_budget(
        self, small_budget: float
    ) -> None:
        """Hang in the GPU capture: bounded, and the CPU is not left frozen."""
        service, transport = _make_wedged_service("gpu.buffer.texture")

        started = time.perf_counter()
        result = await asyncio.wait_for(service.dump_texture(), timeout=_HANG_CEILING_S)
        elapsed = time.perf_counter() - started

        assert result == b""
        # The pause preamble completed before the capture hung — the hang
        # really is in the GPU call, not an earlier failure.
        assert "cpu.stepping" in _faf_events(transport)
        # The with_stepping finally block still attempts a resume after the
        # budget trip — the CPU must not be left frozen in STEPPING.
        assert "cpu.resume" in _faf_events(transport)
        # Lower bound: the wait consumed the budget (not an early error);
        # upper bound: wait_for would have raised TimeoutError without it.
        assert small_budget * 0.75 <= elapsed < _HANG_CEILING_S, (
            f"dump_texture returned after {elapsed:.3f}s with a wedged link; "
            f"expected the {small_budget}s budget to end the wait."
        )

    async def test_wedged_clut_capture_returns_empty_within_budget(
        self, small_budget: float
    ) -> None:
        """Hang in the GPU capture (clut): bounded, and the CPU not left frozen."""
        service, transport = _make_wedged_service("gpu.buffer.clut")

        started = time.perf_counter()
        result = await asyncio.wait_for(service.dump_clut(), timeout=_HANG_CEILING_S)
        elapsed = time.perf_counter() - started

        assert result == b""
        assert "cpu.stepping" in _faf_events(transport)
        assert "cpu.resume" in _faf_events(transport)
        assert small_budget * 0.75 <= elapsed < _HANG_CEILING_S, (
            f"dump_clut returned after {elapsed:.3f}s with a wedged link; "
            f"expected the {small_budget}s budget to end the wait."
        )

    async def test_wedged_preamble_returns_empty_within_budget(self, small_budget: float) -> None:
        """Hang BEFORE the capture (stepping preamble probe): also bounded.

        In the wedged run the preamble alone burned ~10s (entry probe 5s
        + pause probe 1s + pause confirmation 3.5s). The total budget
        must cover it — not just the GPU call.
        """
        service, _transport = _make_wedged_service("cpu.status")

        started = time.perf_counter()
        result = await asyncio.wait_for(service.dump_texture(), timeout=_HANG_CEILING_S)
        elapsed = time.perf_counter() - started

        assert result == b""
        assert small_budget * 0.75 <= elapsed < _HANG_CEILING_S, (
            f"dump_texture returned after {elapsed:.3f}s with a wedged "
            f"stepping preamble; expected the {small_budget}s budget to "
            "cover the preamble too."
        )


class TestG15cContractsUnchanged:
    """G-15c: the budget must not change existing contracts."""

    async def test_successful_texture_capture_still_returns_png(
        self, transport: FakeTransport, client: PpssppDebugClient
    ) -> None:
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        transport.set_response("gpu.buffer.texture", {"uri": _make_data_uri(png_bytes)})
        service = CaptureService(client, transport=transport)

        assert await service.dump_texture(level=0) == png_bytes

    async def test_successful_clut_capture_still_returns_png(
        self, transport: FakeTransport, client: PpssppDebugClient
    ) -> None:
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        transport.set_response("gpu.buffer.clut", {"uri": _make_data_uri(png_bytes)})
        service = CaptureService(client, transport=transport)

        assert await service.dump_clut() == png_bytes

    async def test_non_timeout_failure_still_returns_empty(
        self, transport: FakeTransport, client: PpssppDebugClient
    ) -> None:
        """A transport failure short of the budget keeps the b"" contract."""

        def _boom(**_: Any) -> dict[str, Any]:
            raise RuntimeError("gpu capture exploded")

        transport.set_response("gpu.buffer.texture", _boom)
        service = CaptureService(client, transport=transport)

        assert await service.dump_texture(level=0) == b""
