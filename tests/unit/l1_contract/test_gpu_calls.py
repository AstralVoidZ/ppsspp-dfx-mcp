"""L1 contract tests for GPU buffer / stats / record + memory_info methods.

Anchors (PPSSPP C++ source):
- gpu.buffer.renderColor: GPUBufferSubscriber (type + alpha + stackWidth)
- gpu.buffer.renderDepth: GPUBufferSubscriber (type + alpha + stackWidth)
- gpu.buffer.renderStencil: GPUBufferSubscriber (type + alpha + stackWidth)
- gpu.buffer.texture: GPUBufferSubscriber.cpp:L378-386
  (level + type + alpha + stackWidth; transport timeout=15.0)
- gpu.buffer.clut: GPUBufferSubscriber.cpp:L406-412
  (type + alpha + stackWidth)
- gpu.stats.get: async ticketed (transport timeout; no game-side params)
- gpu.record.dump: GPURecordSubscriber.cpp:L93
  (async ticketed; transport timeout; no game-side params)
- memory.info.search: MemoryInfoSubscriber.cpp:L52, L324-393
  (required `match`; optional address / end / type)

L1 tests assert pure forwarding behavior: each DebugClient method
forwards the correct PPSSPP WebSocket event name + parameters, and
passes through the response verbatim (or extracts the documented
field). Uses the canonical FakeTransport (configured via the
`transport` and `client` fixtures in conftest.py).
"""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp.errors import CpuStateError


class TestGpuBufferContract:
    """L1 contract: gpu.buffer.* methods forward correct event + params."""

    @pytest.mark.asyncio
    async def test_render_color_default_params(self, client, transport):
        """L1 anchor: GPUBufferSubscriber (gpu.buffer.renderColor).

        render_color() with defaults forwards type="uri", alpha=False,
        stackWidth=0 — the PPSSPP `gpu.buffer.renderColor` contract
        defaults.
        """
        transport.set_response("gpu.buffer.renderColor", {"uri": "data:image/png;base64,..."})
        await client.render_color()
        assert transport.calls[-1][0] == "gpu.buffer.renderColor"
        assert transport.calls[-1][1] == {
            "type": "uri",
            "alpha": False,
            "stackWidth": 0,
        }

    @pytest.mark.asyncio
    async def test_render_depth_forwards_event_and_params(self, client, transport):
        """建议2 fix (2026-09-06): render_depth existed only as a docstring
        promise — CaptureService.dump_buffer(target='depth') used to die on
        AttributeError masked as "Unsupported target". Pins the real
        forwarding contract (gpu.buffer.renderDepth)."""
        transport.set_response("gpu.buffer.renderDepth", {"uri": "data:..."})
        await client.render_depth(output_type="uri", alpha=True, stackWidth=8)
        assert transport.calls[-1][0] == "gpu.buffer.renderDepth"
        assert transport.calls[-1][1] == {
            "type": "uri",
            "alpha": True,
            "stackWidth": 8,
        }

    @pytest.mark.asyncio
    async def test_render_stencil_forwards_event_and_params(self, client, transport):
        """建议2 fix: stencil twin of render_depth (gpu.buffer.renderStencil)."""
        transport.set_response("gpu.buffer.renderStencil", {"uri": "data:..."})
        await client.render_stencil()
        assert transport.calls[-1][0] == "gpu.buffer.renderStencil"
        assert transport.calls[-1][1] == {
            "type": "uri",
            "alpha": False,
            "stackWidth": 0,
        }

    @pytest.mark.asyncio
    async def test_render_color_custom_params(self, client, transport):
        """L1 anchor: GPUBufferSubscriber (gpu.buffer.renderColor).

        render_color(output_type="base64", alpha=True, stackWidth=512)
        forwards all three params verbatim.
        """
        transport.set_response("gpu.buffer.renderColor", {"data": "..."})
        await client.render_color(output_type="base64", alpha=True, stackWidth=512)
        assert transport.calls[-1][0] == "gpu.buffer.renderColor"
        assert transport.calls[-1][1] == {
            "type": "base64",
            "alpha": True,
            "stackWidth": 512,
        }

    @pytest.mark.asyncio
    async def test_texture_includes_level_and_default_params(self, client, transport):
        """L1 anchor: GPUBufferSubscriber.cpp:L378-386 (gpu.buffer.texture).

        texture(level=2, type="base64", alpha=True, stackWidth=256)
        forwards all four params to the `gpu.buffer.texture` WS event.
        The transport-level timeout=15.0 (longer than the default 5.0s
        because GPU_GetCurrentTexture can be slow) is consumed by
        FakeTransport.call's named `timeout` parameter and does NOT
        appear in the recorded `**params`.
        """
        transport.set_response("gpu.buffer.texture", {"data": "..."})
        await client.texture(level=2, output_type="base64", alpha=True, stackWidth=256)
        assert transport.calls[-1][0] == "gpu.buffer.texture"
        assert transport.calls[-1][1] == {
            "level": 2,
            "type": "base64",
            "alpha": True,
            "stackWidth": 256,
        }

    @pytest.mark.asyncio
    async def test_clut_forwards_event_and_params(self, client, transport):
        """L1 anchor: GPUBufferSubscriber.cpp:L406-412 (gpu.buffer.clut).

        clut() with defaults forwards type="uri", alpha=False,
        stackWidth=0 to the `gpu.buffer.clut` WS event.
        """
        transport.set_response("gpu.buffer.clut", {"uri": "..."})
        await client.clut()
        assert transport.calls[-1][0] == "gpu.buffer.clut"
        assert transport.calls[-1][1] == {
            "type": "uri",
            "alpha": False,
            "stackWidth": 0,
        }


class TestGpuStatsAndRecordContract:
    """L1 contract: gpu.stats.get + gpu.record.dump (async ticketed)."""

    @pytest.mark.asyncio
    async def test_gpu_stats_forwards_event(self, client, transport):
        """L1 anchor: gpu.stats.get (async ticketed, no game-side params).

        gpu_stats() forwards to the `gpu.stats.get` WS event with no
        game-side params. The transport-level timeout (5.0s default)
        is consumed by FakeTransport.call's named `timeout` parameter
        and does NOT appear in the recorded `**params`. PPSSPP pushes
        stats on the next GPU flip — the timeout is a transport
        concern, not a WS event param.
        """
        transport.set_response(
            "gpu.stats.get",
            {"fps": 60, "vblanksPerSecond": 60, "info": {}, "timing": {}},
        )
        await client.gpu_stats()
        assert transport.calls[-1][0] == "gpu.stats.get"
        assert transport.calls[-1][1] == {}

    @pytest.mark.asyncio
    async def test_gpu_record_dump_forwards_event(self, client, transport):
        """L1 anchor: GPURecordSubscriber.cpp:L93 (gpu.record.dump).

        gpu_record_dump() forwards to the `gpu.record.dump` WS event
        with no game-side params. The transport-level timeout (5.0s
        default) is consumed by FakeTransport.call's named `timeout`
        parameter and does NOT appear in the recorded `**params`. The
        response contains a `uri` field with a
        `data:application/octet-stream;base64,<payload>` URI (passed
        through verbatim).
        """
        transport.set_response(
            "gpu.record.dump",
            {"uri": "data:application/octet-stream;base64,AAAA"},
        )
        result = await client.gpu_record_dump()
        assert transport.calls[-1][0] == "gpu.record.dump"
        assert transport.calls[-1][1] == {}
        assert result["uri"] == "data:application/octet-stream;base64,AAAA"


class TestGpuStatsRecordRequiredRunning:
    """L1 contract: gpu_stats / gpu_record_dump REQUIRED_RUNNING pre-check.

    Batch 1: DebugClient probes `cpu.status` before issuing the ticketed
    call. If stepping=True, raises CpuStateError immediately instead of
    waiting `timeout` seconds for the silent timeout (no GPU flip while
    CPU is paused — see GPUStatsSubscriber.cpp:98 __DisplayListenFlip).
    """

    @pytest.mark.asyncio
    async def test_gpu_stats_raises_cpu_state_error_when_stepping(self, client, transport):
        """L1 anchor: gpu_stats raises CpuStateError when CPU is stepping.

        Pre-set stepping=True. gpu_stats must raise CpuStateError (not
        wait for the 5-second timeout). The error message includes the
        WS event contract's diagnostic_hint.
        """
        transport.set_state({"stepping": True})
        with pytest.raises(CpuStateError, match="gpu.stats.get"):
            await client.gpu_stats(timeout=0.5)
        # Verify the ticketed call was NOT issued (pre-check short-circuited).
        gpu_stats_calls = [(ev, p) for ev, p in transport.calls if ev == "gpu.stats.get"]
        assert len(gpu_stats_calls) == 0, (
            "gpu_stats must NOT issue the ticketed call when CPU is "
            "stepping — the REQUIRED_RUNNING pre-check must short-circuit."
        )

    @pytest.mark.asyncio
    async def test_gpu_stats_forwards_when_running(self, client, transport):
        """L1 anchor: gpu_stats forwards normally when CPU is running.

        Default fixture state: stepping=False. The pre-check passes
        and the ticketed call is issued.
        """
        transport.set_response(
            "gpu.stats.get",
            {"fps": 60, "vblanksPerSecond": 60, "info": {}, "timing": {}},
        )
        await client.gpu_stats(timeout=0.5)
        gpu_stats_calls = [(ev, p) for ev, p in transport.calls if ev == "gpu.stats.get"]
        assert len(gpu_stats_calls) == 1

    @pytest.mark.asyncio
    async def test_gpu_record_dump_raises_cpu_state_error_when_stepping(self, client, transport):
        """L1 anchor: gpu_record_dump raises CpuStateError when CPU is stepping.

        Pre-set stepping=True. gpu_record_dump must raise CpuStateError
        (not wait for the 5-second timeout). The error message includes
        the WS event contract's diagnostic_hint.
        """
        transport.set_state({"stepping": True})
        with pytest.raises(CpuStateError, match="gpu.record.dump"):
            await client.gpu_record_dump(timeout=0.5)
        # Verify the ticketed call was NOT issued.
        gpu_record_calls = [(ev, p) for ev, p in transport.calls if ev == "gpu.record.dump"]
        assert len(gpu_record_calls) == 0, (
            "gpu_record_dump must NOT issue the ticketed call when CPU is "
            "stepping — the REQUIRED_RUNNING pre-check must short-circuit."
        )

    @pytest.mark.asyncio
    async def test_gpu_record_dump_forwards_when_running(self, client, transport):
        """L1 anchor: gpu_record_dump forwards normally when CPU is running."""
        transport.set_response(
            "gpu.record.dump",
            {"uri": "data:application/octet-stream;base64,AAAA"},
        )
        await client.gpu_record_dump(timeout=0.5)
        gpu_record_calls = [(ev, p) for ev, p in transport.calls if ev == "gpu.record.dump"]
        assert len(gpu_record_calls) == 1


class TestSearchMemoryInfoContract:
    """L1 contract: memory.info.search (required match + optional filters)."""

    @pytest.mark.asyncio
    async def test_required_match_only(self, client, transport):
        """L1 anchor: MemoryInfoSubscriber.cpp:L52, L324-393.

        search_memory_info(match="framebuf") with no optional params
        forwards only the `match` key. Optional params (address / end
        / type) are omitted entirely when None — NOT forwarded as
        null.
        """
        transport.set_response("memory.info.search", {"extent": None})
        await client.search_memory_info(match="framebuf")
        assert transport.calls[-1][0] == "memory.info.search"
        assert transport.calls[-1][1] == {"match": "framebuf"}

    @pytest.mark.asyncio
    async def test_with_optional_params(self, client, transport):
        """L1 anchor: MemoryInfoSubscriber.cpp:L52, L324-393.

        search_memory_info(match="texture", address=0x04000000,
        end=0x04200000, type="texture") forwards all four params
        verbatim. The response `extent` field is passed through
        unchanged.
        """
        extent_payload = {
            "type": "texture",
            "address": 0x04000000,
            "size": 0x10000,
            "tag": "framebuf",
        }
        transport.set_response("memory.info.search", {"extent": extent_payload})
        result = await client.search_memory_info(
            match="texture",
            address=0x04000000,
            end=0x04200000,
            type="texture",
        )
        assert transport.calls[-1][0] == "memory.info.search"
        assert transport.calls[-1][1] == {
            "match": "texture",
            "address": 0x04000000,
            "end": 0x04200000,
            "type": "texture",
        }
        assert result["extent"] == extent_payload
