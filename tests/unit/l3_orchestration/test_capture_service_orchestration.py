"""L3 orchestration tests: CaptureService strategy chain.

Anchors: B.2 spec §4 V023 invariants I15-I17 under orchestration.
Complementary to L4 (which anchors static source-level invariants in
`tests/unit/l4_regression/test_v023_capture_encapsulation.py`).

L3 focus (NOT covered by L4 / unit tests):
- screenshot(source="render") orchestration: with_stepping → render_color
  (NO direct transport access — V023 I15 orchestration)
- screenshot(source="output") orchestration: with_stepping →
  transport.call("gpu.buffer.screenshot") (V023 fix: explicit transport)
- safe_screenshot three-tier degradation chain order
  (WM → PrintWindow → VRAM; first success stops the chain)
- safe_screenshot all-fail returns empty bytes (orchestration integrity)
- dump_texture orchestration: with_stepping → texture (NO transport)

V023 invariants (B.2 §4.4) anchored here:
- I15 (orchestration): screenshot(render) / safe_screenshot / dump_texture
  use client methods only — never self._transport.call
- I16 (orchestration): screenshot(output) is the ONLY path that touches
  gpu.buffer.screenshot, and it goes through self._transport (NOT
  self._client._transport)
- I17 (orchestration): CaptureService requires explicit transport for
  the output path; the same transport backs the client
"""

from __future__ import annotations

import base64

import pytest
from fake_transport import FakeTransport

from ppsspp_dfx_mcp.service.capture import CaptureService
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

# ============================================================================
# Fixtures (real PpssppDebugClient + FakeTransport for orchestration)
# ============================================================================


@pytest.fixture
def cap_transport() -> FakeTransport:
    """FakeTransport with stepping state transitions configured."""
    t = FakeTransport()
    t.set_state({"stepping": False})
    t.set_faf_handler("cpu.stepping", lambda t, **_: t.set_state({"stepping": True}))
    t.set_faf_handler("cpu.resume", lambda t, **_: t.set_state({"stepping": False}))
    return t


@pytest.fixture
def cap_client(cap_transport: FakeTransport) -> PpssppDebugClient:
    """PpssppDebugClient backed by cap_transport."""
    return PpssppDebugClient(cap_transport)


@pytest.fixture
def cap_service(cap_client: PpssppDebugClient, cap_transport: FakeTransport) -> CaptureService:
    """CaptureService with explicit transport injection (V023 fix)."""
    return CaptureService(cap_client, transport=cap_transport)


def _make_data_uri(png_bytes: bytes) -> str:
    """Build a data URI string for PNG bytes."""
    b64 = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{b64}"


# ============================================================================
# screenshot(source="render") orchestration (V023 I15)
# ============================================================================


class TestScreenshotRenderOrchestration:
    """L3: screenshot(source="render") uses client methods only.

    V023 I15 (orchestration): the render path uses
    client.with_stepping + client.render_color — it does NOT access
    self._transport directly. L4 anchors this via static source check;
    L3 anchors it via runtime call inspection.
    """

    async def test_render_calls_render_color_not_transport(self, cap_service, cap_transport):
        """render path: client.render_color called, transport.call NOT called."""
        # Configure render_color to return a valid data URI
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        cap_transport.set_response("gpu.buffer.renderColor", {"uri": _make_data_uri(png_bytes)})

        result = await cap_service.screenshot(source="render")

        assert result == png_bytes
        # Verify gpu.buffer.renderColor was called (through client)
        call_events = [ev for ev, _ in cap_transport.calls]
        assert "gpu.buffer.renderColor" in call_events
        # Verify gpu.buffer.screenshot was NOT called (render path
        # must not touch the CRASH-RISK output path)
        assert "gpu.buffer.screenshot" not in call_events, (
            "screenshot(source='render') must NOT call "
            "gpu.buffer.screenshot (V023 I16 orchestration — render "
            "path is isolated from the CRASH-RISK output path)."
        )

    async def test_render_uses_with_stepping(self, cap_service, cap_transport):
        """render path: client.with_stepping pauses/resumes CPU."""
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        cap_transport.set_response("gpu.buffer.renderColor", {"uri": _make_data_uri(png_bytes)})

        await cap_service.screenshot(source="render")

        events = [ev for ev, _ in cap_transport.fire_and_forget_calls]
        assert "cpu.stepping" in events
        assert "cpu.resume" in events


# ============================================================================
# screenshot(source="output") orchestration (V023 fix)
# ============================================================================


class TestScreenshotOutputOrchestration:
    """L3: screenshot(source="output") uses explicit transport (V023 fix).

    V023 fix: _output_screenshot uses self._transport.call(...) (NOT
    self._client._transport.call(...)). L4 anchors this via static
    source check; L3 anchors it via runtime call inspection — the
    transport that receives the call IS the explicitly-injected one.
    """

    async def test_output_calls_gpu_buffer_screenshot_via_transport(
        self, cap_service, cap_transport
    ):
        """output path: transport.call("gpu.buffer.screenshot") invoked."""
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        cap_transport.set_response("gpu.buffer.screenshot", {"uri": _make_data_uri(png_bytes)})

        result = await cap_service.screenshot(source="output")

        assert result == png_bytes
        call_events = [ev for ev, _ in cap_transport.calls]
        assert "gpu.buffer.screenshot" in call_events, (
            "screenshot(source='output') must call "
            "gpu.buffer.screenshot via the injected transport."
        )

    async def test_output_uses_with_stepping(self, cap_service, cap_transport):
        """output path: with_stepping preserves CPU state across capture."""
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        cap_transport.set_response("gpu.buffer.screenshot", {"uri": _make_data_uri(png_bytes)})

        assert cap_transport.state.get("stepping") is False  # precondition
        await cap_service.screenshot(source="output")
        assert cap_transport.state.get("stepping") is False, (
            "with_stepping must restore CPU to running after output path."
        )


# ============================================================================
# safe_screenshot three-tier degradation chain (V023 orchestration)
# ============================================================================


class TestSafeScreenshotDegradationChain:
    """L3: safe_screenshot three-tier chain: WM → PrintWindow → VRAM.

    The chain stops at the first successful strategy. L3 anchors:
    1. First strategy success → no fallback called
    2. All strategies fail → returns b"" (no exception)
    3. With_stepping wraps the entire chain (preserve_state)
    """

    async def test_first_strategy_success_stops_chain(self, cap_service, monkeypatch):
        """WM strategy success: PrintWindow / VRAM NOT called.

        Monkey-patch _wm_command_screenshot to return bytes; verify
        _print_window_screenshot and _vram_screenshot are NOT invoked.
        """
        png_bytes = b"\x89PNG" + b"\x00" * 20
        call_log: list[str] = []

        async def mock_wm(self):
            call_log.append("wm")
            return png_bytes

        async def mock_printwindow(self):
            call_log.append("printwindow")
            return b""

        async def mock_vram(self):
            call_log.append("vram")
            return b""

        monkeypatch.setattr(CaptureService, "_wm_command_screenshot", mock_wm)
        monkeypatch.setattr(CaptureService, "_print_window_screenshot", mock_printwindow)
        monkeypatch.setattr(CaptureService, "_vram_screenshot", mock_vram)

        result = await cap_service.safe_screenshot()

        assert result == png_bytes
        assert call_log == ["wm"], f"First strategy success must stop chain. Got {call_log}."

    async def test_all_strategies_fail_returns_empty(self, cap_service, monkeypatch):
        """All three strategies fail: returns b"" (no exception)."""
        call_log: list[str] = []

        async def mock_wm(self):
            call_log.append("wm")
            return b""

        async def mock_printwindow(self):
            call_log.append("printwindow")
            return b""

        async def mock_vram(self):
            call_log.append("vram")
            return b""

        monkeypatch.setattr(CaptureService, "_wm_command_screenshot", mock_wm)
        monkeypatch.setattr(CaptureService, "_print_window_screenshot", mock_printwindow)
        monkeypatch.setattr(CaptureService, "_vram_screenshot", mock_vram)

        result = await cap_service.safe_screenshot()

        assert result == b""
        assert call_log == ["wm", "printwindow", "vram"], (
            f"All three strategies must be tried in order. Got {call_log}."
        )

    async def test_strategy_exception_continues_chain(self, cap_service, monkeypatch):
        """Strategy raises exception: chain continues to next strategy."""
        call_log: list[str] = []

        async def mock_wm(self):
            call_log.append("wm")
            raise RuntimeError("wm failed")

        async def mock_printwindow(self):
            call_log.append("printwindow")
            raise RuntimeError("printwindow failed")

        async def mock_vram(self):
            call_log.append("vram")
            return b"\x89PNG" + b"\x00" * 20

        monkeypatch.setattr(CaptureService, "_wm_command_screenshot", mock_wm)
        monkeypatch.setattr(CaptureService, "_print_window_screenshot", mock_printwindow)
        monkeypatch.setattr(CaptureService, "_vram_screenshot", mock_vram)

        result = await cap_service.safe_screenshot()

        assert result == b"\x89PNG" + b"\x00" * 20
        assert call_log == ["wm", "printwindow", "vram"], (
            "Exception in early strategy must not stop the chain — "
            f"VRAM must be tried. Got {call_log}."
        )


# ============================================================================
# dump_texture orchestration (V023 I15 — no transport access)
# ============================================================================


class TestDumpTextureOrchestration:
    """L3: dump_texture uses client methods only (no transport).

    V023 I15 (orchestration): dump_texture uses client.with_stepping +
    client.texture — it does NOT access self._transport. L3 anchors
    this via runtime call inspection.
    """

    async def test_dump_texture_calls_texture_not_transport(self, cap_service, cap_transport):
        """dump_texture: client.texture called, transport.call NOT called."""
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        cap_transport.set_response("gpu.buffer.texture", {"uri": _make_data_uri(png_bytes)})

        result = await cap_service.dump_texture(level=0)

        assert result == png_bytes
        call_events = [ev for ev, _ in cap_transport.calls]
        assert "gpu.buffer.texture" in call_events
        # dump_texture must NOT touch transport directly
        assert "gpu.buffer.screenshot" not in call_events, (
            "dump_texture must NOT call gpu.buffer.screenshot (V023 I15 "
            "orchestration — only _output_screenshot may touch transport)."
        )

    async def test_dump_texture_uses_with_stepping(self, cap_service, cap_transport):
        """dump_texture: with_stepping pauses/resumes CPU."""
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
        cap_transport.set_response("gpu.buffer.texture", {"uri": _make_data_uri(png_bytes)})

        await cap_service.dump_texture(level=0)

        events = [ev for ev, _ in cap_transport.fire_and_forget_calls]
        assert "cpu.stepping" in events
        assert "cpu.resume" in events
