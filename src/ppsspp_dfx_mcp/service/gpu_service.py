"""GPU / render methods — extracted from PpssppDebugClient.

Buffer capture, the REQUIRED_RUNNING pre-check, and the ticketed
``gpu.stats.get`` / ``gpu.record.dump`` pulls. Each function takes the
``WsTransport`` it needs, so the logic is independent of the client
object; ``PpssppDebugClient`` keeps thin delegating methods with
identical signatures.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from ppsspp_dfx_mcp.core.transport import WsTransport
from ppsspp_dfx_mcp.core.ws_contract import get_contract
from ppsspp_dfx_mcp.errors import CpuStateError


async def render_color(
    transport: WsTransport,
    output_type: Literal["uri", "base64"] = "uri",
    alpha: bool = False,
    stackWidth: int = 0,
) -> dict[str, Any]:
    """Capture GPU render color buffer.

    Extracted implementation of ``PpssppDebugClient.render_color``.
    """
    return await transport.call(
        "gpu.buffer.renderColor",
        type=output_type,
        alpha=alpha,
        stackWidth=stackWidth,
    )


async def texture(
    transport: WsTransport,
    level: int = 0,
    output_type: Literal["uri", "base64"] = "uri",
    alpha: bool = False,
    stackWidth: int = 0,
) -> dict[str, Any]:
    """Capture the currently-bound GPU texture.

    Extracted implementation of ``PpssppDebugClient.texture``.
    """
    return await transport.call(
        "gpu.buffer.texture",
        level=level,
        type=output_type,
        alpha=alpha,
        stackWidth=stackWidth,
        timeout=15.0,
    )


async def render_depth(
    transport: WsTransport,
    output_type: Literal["uri", "base64"] = "uri",
    alpha: bool = False,
    stackWidth: int = 0,
) -> dict[str, Any]:
    """Capture the GPU depth buffer.

    Extracted implementation of ``PpssppDebugClient.render_depth``.
    """
    return await transport.call(
        "gpu.buffer.renderDepth",
        type=output_type,
        alpha=alpha,
        stackWidth=stackWidth,
    )


async def render_stencil(
    transport: WsTransport,
    output_type: Literal["uri", "base64"] = "uri",
    alpha: bool = False,
    stackWidth: int = 0,
) -> dict[str, Any]:
    """Capture the GPU stencil buffer. See render_depth.

    Extracted implementation of ``PpssppDebugClient.render_stencil``.
    """
    return await transport.call(
        "gpu.buffer.renderStencil",
        type=output_type,
        alpha=alpha,
        stackWidth=stackWidth,
    )


async def clut(
    transport: WsTransport,
    output_type: Literal["uri", "base64"] = "uri",
    alpha: bool = False,
    stackWidth: int = 0,
) -> dict[str, Any]:
    """Capture the currently-bound GPU CLUT (palette).

    Extracted implementation of ``PpssppDebugClient.clut``.
    """
    return await transport.call(
        "gpu.buffer.clut",
        type=output_type,
        alpha=alpha,
        stackWidth=stackWidth,
    )


async def require_running(transport: WsTransport, event: str) -> None:
    """Pre-check CPU state for a REQUIRED_RUNNING event.

    Extracted implementation of ``PpssppDebugClient._require_running``
    — see that method for the full decision-matrix docstring.
    """
    # First probe — record ticks0 + stepping0.
    try:
        status0 = await transport.call("cpu.status")
    except (ConnectionRefusedError, OSError):
        # Transport-level failure (WS disconnected / port unreachable):
        # re-raise so to_tool_error can classify it as WsDisconnected
        # immediately, rather than letting the ticketed call time out.
        raise
    except Exception:
        # Other probe failures (e.g. PPSSPP internal error): fall
        # through and let the ticketed call surface the underlying error.
        return
    stepping0 = status0.get("stepping") is True
    ticks0 = status0.get("ticks")

    # 50ms sleep — long enough for ticks to advance on a running
    # CPU (60fps → ~16ms/frame → ~3 frames in 50ms), short enough
    # to keep the pre-check cost negligible.
    await asyncio.sleep(0.05)

    # Second probe — record ticks1 + stepping1.
    try:
        status1 = await transport.call("cpu.status")
    except (ConnectionRefusedError, OSError):
        raise
    except Exception:
        return
    stepping1 = status1.get("stepping") is True
    ticks1 = status1.get("ticks")

    # Decision matrix:
    # 1. stepping=True at either probe → CpuStateError (existing
    #    behavior: stepping field detected pause).
    if stepping0 or stepping1:
        contract = get_contract(event)
        raise CpuStateError(
            f"{event} requires CPU running (not stepping): "
            f"{contract.diagnostic_hint} "
            f"[stepping0={stepping0}, stepping1={stepping1}, "
            f"ticks0={ticks0}, ticks1={ticks1}, 50ms probe]"
        )
    # 2. stepping=False at both probes AND ticks present AND
    #    unchanged → CpuStateError (breakpoint pause suspected but
    #    stepping field didn't report — ticks backup validation).
    #    When ticks is None (not provided by transport, e.g.
    #    FakeTransport in tests or older PPSSPP builds), skip the
    #    ticks backup validation and rely on the stepping field only.
    if ticks0 is not None and ticks1 is not None and ticks0 == ticks1:
        raise CpuStateError(
            f"{event} requires CPU running: ticks unchanged "
            f"({ticks0} == {ticks1}) after 50ms, breakpoint pause "
            f"suspected but stepping field didn't report "
            f"[stepping0={stepping0}, stepping1={stepping1}]"
        )
    # 3. stepping=False at both probes AND ticks progressed →
    #    CPU advancing normally. Allow the ticketed call to proceed.


async def gpu_stats(transport: WsTransport, timeout: float = 5.0) -> dict[str, Any]:
    """Query GPU statistics (fps, vblanks, timing).

    Extracted implementation of ``PpssppDebugClient.gpu_stats`` — see
    that method for the full contract docstring.
    """
    await require_running(transport, "gpu.stats.get")
    return await transport.call("gpu.stats.get", timeout=timeout)


async def gpu_record_dump(transport: WsTransport, timeout: float = 5.0) -> dict[str, Any]:
    """Capture a GPU record dump (GE command stream for one frame).

    Extracted implementation of ``PpssppDebugClient.gpu_record_dump`` —
    see that method for the full contract docstring.
    """
    await require_running(transport, "gpu.record.dump")
    return await transport.call("gpu.record.dump", timeout=timeout)
