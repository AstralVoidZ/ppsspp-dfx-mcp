"""CaptureService — screenshot/texture/buffer capture with strategy chain.

Composes a PpssppDebugClient AND a WsTransport. All transport-level
access goes through the explicitly-injected transport (V023 fix), except
the explicitly-allowed `screenshot(source="output")` path which uses
`self._transport.call("gpu.buffer.screenshot")` (CRASH-RISK).

Screenshot strategies:
- `screenshot(source="render")` — NEW behavior: with_stepping + renderColor.
  Replaces the previous `mode=auto` default path.
- `screenshot(source="output")` — calls gpu.buffer.screenshot directly
  (CRASH-RISK: can crash PPSSPP on certain games).
- `safe_screenshot()` — three-tier Win32 fallback (preserves the previous
  `mode=auto` behavior): WM_COMMAND → PrintWindow → VRAM.

Texture/buffer dump:
- `dump_texture()` — with_stepping + client.texture(level, output_type="uri") + data URI extract.
  Captures currently-bound texture (PPSSPP does not support address-based capture).
- `dump_buffer()` — delegates to render_depth/render_stencil/clut based
  on target. `output_type` param applies to all targets (clut now also accepts output_type).

Win32 GDI interop is private to this class. All Win32 code is
Windows-only and wrapped with try/except returning b"" on failure.

V023 (B.2 §4): transport is now explicitly injected via __init__ rather
than accessed via `_client._transport` (encapsulation violation). The
transport parameter is required — callers (e.g. session_capture) must
pass the same WsTransport that backs the PpssppDebugClient.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import sys
import time
from pathlib import Path
from typing import Any, Literal

from ppsspp_dfx_mcp.core.transport import WsTransport
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

logger = logging.getLogger(__name__)

# The WM_COMMAND strategy waits for PPSSPP to finish writing a screenshot
# file. Both the appearance scan and the per-file settle wait are bounded by
# this deadline. PPSSPP can leave a zero-byte .png behind (killed mid-write,
# disk full, or a stale file from an earlier run); the previous unbounded
# "two consecutive equal non-zero sizes" loop then spun forever while
# safe_screenshot held the session lock with the CPU halted in STEPPING.
_WM_SCREENSHOT_WAIT_S = 5.0
_WM_SCREENSHOT_POLL_S = 0.05

# Dedicated TOTAL budget (seconds) for one dump operation — the stepping
# preamble (state probe + pause + confirmation), the GPU capture and the
# resume — shared by dump_texture() and dump_clut().
#
# Why a total budget instead of narrowing individual call timeouts: the
# deep-test wedged run (`mcp_test_report/logs/_run5_fullseq_stuck`) spent
# 22-32s on the EMPTY-capture path, and the server stderr shows that was
# not one constant but four stacked fallback budgets:
#   - with_stepping preamble: entry probe 5s (+0.5s stepping probe) +
#     pause probe 0.5s (+0.5s) + pause confirmation 3s (+0.5s) ~= 10s;
#   - the texture call's own explicit 15s timeout (+0.5s probe), plus a
#     failed 3s resume after the body raised ~= 22s (P3-36: 22.06s);
#   - when the debugger socket had dropped, the transport-level
#     reconnect (connect + 15s version handshake) burned another ~22s
#     BEFORE the capture started (P3-37: 31.74s).
# The reconnect path lives inside transport.call(), so no per-event
# timeout can bound it; one budget around the whole operation can.
#
# Value: measured healthy round trips (T070 probes, 2026-10-05, real
# MCP stdio client, v1.20.4): EMPTY capture p50=20.6ms / p95=25.8ms
# (probe 1: n=20, texture 10 + clut 10, title screen) and p50=33.3ms /
# max=37.7ms (probe 2: n=16 across 8 start/cross rounds — the probes
# never reached a rendering scene, so no successful-capture sample was
# obtained; that path adds GPU readback + PNG encoding on top of the
# same preamble). 3.0s keeps a ~80x margin over the slowest measured
# round trip while staying an order of magnitude below the MCP client's
# 30s read timeout (G-15 / FR-015).
_DUMP_TOTAL_BUDGET_S = 3.0


# ---------- Win32 GDI types (migrated from the legacy PpssppClient) ----------

# 两个结构体类是「可选」的：非 Windows 上 ctypes.wintypes 导入会失败，名字必须
# 能合法地取 None（使用点见 _capture_window 的空值守卫）。故先声明为可选别名，
# 真类定义在 try 内，再赋给别名——直接给类名赋 None 会被类型检查器判为错误。
_BITMAPINFOHEADER: type[Any] | None
_BITMAPINFO: type[Any] | None

try:
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    class _BitmapInfoHeaderStruct(_ctypes.Structure):
        _fields_ = [
            ("biSize", _wintypes.DWORD),
            ("biWidth", _wintypes.LONG),
            ("biHeight", _wintypes.LONG),
            ("biPlanes", _wintypes.WORD),
            ("biBitCount", _wintypes.WORD),
            ("biCompression", _wintypes.DWORD),
            ("biSizeImage", _wintypes.DWORD),
            ("biXPelsPerMeter", _wintypes.LONG),
            ("biYPelsPerMeter", _wintypes.LONG),
            ("biClrUsed", _wintypes.DWORD),
            ("biClrImportant", _wintypes.DWORD),
        ]

    class _BitmapInfoStruct(_ctypes.Structure):
        _fields_ = [("bmiHeader", _BitmapInfoHeaderStruct)]

    _BITMAPINFOHEADER = _BitmapInfoHeaderStruct
    _BITMAPINFO = _BitmapInfoStruct
except Exception as e:
    logger.debug("Win32 ctypes structure init failed: %s", e)
    _BITMAPINFOHEADER = None
    _BITMAPINFO = None


def _vram_candidate_to_png(raw: bytes, w: int, h: int, stride: int, bpp: int) -> bytes | None:
    """Convert one VRAM framebuffer candidate to PNG (CPU-bound, thread-run).

    Returns None when the candidate is rejected as blank (<30% non-zero
    bytes). Pure function over ``raw`` so ``_vram_screenshot`` can run it
    via ``asyncio.to_thread`` without touching shared state.
    """
    size = stride * h * bpp
    nonzero = sum(1 for b in raw if b != 0)
    if nonzero < size * 0.3:
        return None
    from PIL import Image  # optional dependency — see _vram_screenshot

    img = Image.new("RGB", (w, h))
    pixels = img.load()
    for y in range(h):
        for x in range(w):
            offset = (y * stride + x) * bpp
            pixels[x, y] = (raw[offset], raw[offset + 1], raw[offset + 2])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class CaptureService:
    """Screenshot and texture capture service.

    Composes a PpssppDebugClient AND a WsTransport. Both are injected;
    CaptureService does NOT own them. The transport is used only for
    the CRASH-RISK `gpu.buffer.screenshot` path (V023 fix: explicit
    injection replaces the prior `_client._transport` encapsulation
    violation). All other transport access goes through the client.
    """

    def __init__(
        self,
        client: PpssppDebugClient,
        transport: WsTransport,
    ) -> None:
        """Initialize CaptureService.

        Args:
            client: PpssppDebugClient for domain method calls
                (render_color, texture, read_bytes, with_stepping, etc.).
            transport: WsTransport for the CRASH-RISK
                `gpu.buffer.screenshot` path. Must be the same transport
                that backs the client (V023 I17 — required parameter).

        V023 invariants (B.2 §4.4):
        - I15: CaptureService does NOT access `self._client._transport`
        - I16: `gpu.buffer.screenshot` is NOT exposed on PpssppDebugClient
        - I17: `transport` is a required parameter (no default)
        """
        self._client = client
        self._transport = transport

    # ======================================================================
    # Screenshot (tasks 4.2, 4.3) — new behavior
    # ======================================================================

    async def screenshot(
        self,
        source: Literal["render", "output"] = "render",
    ) -> bytes:
        """Take a screenshot.

        Args:
            source: capture strategy.
                - "render" (default): with_stepping + client.render_color().
                  New behavior, replaces the previous mode=auto default.
                - "output": calls `gpu.buffer.screenshot` directly.
                  CRASH-RISK: `gpu.buffer.screenshot` can crash PPSSPP on
                  certain games. Use only when explicitly required.

        Returns:
            PNG bytes (empty bytes on failure).

        Breaking change: parameter name changed from `mode` to `source`.
        The previous `mode=auto/wm_command/printwindow/vram` behavior is
        preserved via `safe_screenshot()`.
        """
        if source == "render":
            return await self._render_screenshot()
        if source == "output":
            return await self._output_screenshot()
        raise ValueError(f"Unsupported screenshot source: {source!r}")

    async def _render_screenshot(self) -> bytes:
        """Capture via with_stepping + render_color (NEW default path)."""
        try:
            async with self._client.with_stepping():
                resp = await self._client.render_color(output_type="uri")
            return _extract_png_from_data_uri(resp.get("uri", ""))
        except Exception as e:
            logger.warning("render_color screenshot failed: %s", e, exc_info=True)
            return b""

    async def _output_screenshot(
        self,
        alpha: bool = False,
        stackWidth: int = 0,
    ) -> bytes:
        """Capture via gpu.buffer.screenshot directly.

        CRASH-RISK: `gpu.buffer.screenshot` can crash PPSSPP on certain
        games. This is the only path in CaptureService that bypasses the
        DebugClient domain API and accesses the transport directly, as
        required by the spec (CaptureService SHALL NOT expose this on
        DebugClient).

        V023 fix: transport is accessed via the explicitly-injected
        `self._transport` (NOT `self._client._transport` — encapsulation
        violation fixed per B.2 §4).

        Prior-pause: wrapped in `with_stepping` so the CPU pause state
        is preserved across the capture (paused before → paused after;
        running before → running after). The gpu.buffer.screenshot call
        itself does not require CPU running.

        Args:
            alpha: include alpha channel in capture (default False).
                See PPSSPP GPUBufferSubscriber.cpp:L194-222.
            stackWidth: stacked image width (default 0). See PPSSPP
                GPUBufferSubscriber.cpp:L194-222.
        """
        try:
            async with self._client.with_stepping():
                resp = await self._transport.call(
                    "gpu.buffer.screenshot",
                    type="uri",
                    alpha=alpha,
                    stackWidth=stackWidth,
                )
        except Exception as e:
            logger.warning(
                "gpu.buffer.screenshot failed (CRASH-RISK path): %s",
                e,
                exc_info=True,
            )
            return b""
        return _extract_png_from_data_uri(resp.get("uri", ""))

    # ======================================================================
    # safe_screenshot (task 4.4) — three-tier Win32 fallback
    # ======================================================================

    async def safe_screenshot(self) -> bytes:
        """Safe screenshot: three-strategy fallback.

        Strategy 1 (PRIMARY): WM_COMMAND → PPSSPP internal screenshot file.
          - Full post-processing, accurate colors, no occlusion.
          - Windows-only.

        Strategy 2 (FALLBACK): PrintWindow + PW_RENDERFULLCONTENT.
          - Occlusion-safe (reads from DWM redirection surface).
          - GPU content depends on backend (DX/OpenGL OK, Vulkan uncertain).
          - Windows 8.1+.

        Strategy 3 (LAST RESORT): VRAM direct read.
          - Instant, cross-platform, but colors are unreliable.
          - Only useful for structural reference.

        Prior-pause: the three-tier fallback is attempted with with_stepping
        first. If with_stepping fails (e.g. WebSocket disconnected, pause
        failed), the strategies still run WITHOUT stepping — they are
        Win32 GDI / VRAM reads that do not depend on CPU state (PPSSPP's
        UI thread is independent of CPU emulation, so WM_COMMAND /
        PrintWindow still respond when the CPU is paused or even when the
        WebSocket is down).
        """
        stepping_failed = False
        try:
            async with self._client.with_stepping():
                for strategy_fn in (
                    self._wm_command_screenshot,
                    self._print_window_screenshot,
                    self._vram_screenshot,
                ):
                    try:
                        data = await strategy_fn()
                    except Exception as e:
                        logger.warning(
                            "safe_screenshot strategy %s failed: %s",
                            strategy_fn.__name__,
                            e,
                        )
                        continue
                    if data:
                        return data
        except Exception as e:
            # with_stepping failed (pause failed, WebSocket down, etc.).
            # Do NOT give up — the Win32/VRAM strategies work regardless
            # of CPU state. Log and fall through to strategy execution
            # without stepping.
            logger.warning(
                "safe_screenshot with_stepping failed: %s; attempting strategies without stepping",
                e,
            )
            stepping_failed = True

        # Fallback: run strategies without stepping when with_stepping
        # failed. Win32 GDI and VRAM reads do not require CPU pause.
        if stepping_failed:
            for strategy_fn in (
                self._wm_command_screenshot,
                self._print_window_screenshot,
                self._vram_screenshot,
            ):
                try:
                    data = await strategy_fn()
                except Exception as e:
                    logger.warning(
                        "safe_screenshot strategy %s failed (no stepping): %s",
                        strategy_fn.__name__,
                        e,
                    )
                    continue
                if data:
                    return data

        return b""

    # ======================================================================
    # Win32 GDI screenshot strategies (tasks 4.5, 4.6) — private
    # ======================================================================

    def _find_ppsspp_window(self, pid: int | None = None) -> int:
        """Find PPSSPP main window handle (Windows only). Returns 0 if not found.

        Uses window class name 'PPSSPPWnd' for filtering instead of title
        matching. Title-based matching can false-positive on Windows Terminal
        hosting windows (CASCADIA_HOSTING_WINDOW_CLASS) whose title may contain
        'PPSSPP'. The class name 'PPSSPPWnd' is PPSSPP-exclusive.

        When `pid` is given, only windows owned by that process match —
        with two PPSSPP sessions running, "first PPSSPPWnd in Z order"
        may belong to the OTHER session, and a WM_COMMAND screenshot
        would capture the wrong game with no warning.

        Returns 0 on non-Windows platforms or any exception.
        """
        if sys.platform != "win32":
            return 0
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            ppsspp_hwnd = 0

            def enum_callback(hwnd, _):
                nonlocal ppsspp_hwnd
                if ppsspp_hwnd:
                    return True
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buf, 256)
                if class_buf.value != "PPSSPPWnd":
                    return True
                if pid is not None:
                    window_pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
                    if window_pid.value != pid:
                        return True  # a different session's PPSSPP
                ppsspp_hwnd = hwnd
                return False

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
            return ppsspp_hwnd
        except Exception as e:
            logger.debug("_find_ppsspp_window failed: %s", e)
            return 0

    async def _wm_command_screenshot(self) -> bytes:
        """Screenshot via SendMessage WM_COMMAND → PPSSPP internal screenshot.

        PRIMARY strategy in safe_screenshot(). Triggers the same
        g_TakeScreenshot path as F12, then reads the saved file from
        memstick/PSP/SCREENSHOT/.

        Windows-only. Most reliable: full post-processing, no occlusion.
        Returns b"" on non-Windows or any failure.
        """
        if sys.platform != "win32":
            return b""
        try:
            import asyncio
            import ctypes
            from pathlib import Path

            # Filter windows by the session's PID — with two PPSSPP
            # sessions running, Z-order-first PPSSPPWnd may belong to the
            # other session. None (unknown pid) keeps the legacy
            # first-match behavior.
            hwnd = self._find_ppsspp_window(pid=getattr(self._client, "pid", None))
            if not hwnd:
                return b""

            # Screenshot directory derived from project config.
            # Env override first (PPSSPP_DFX_MEMSTICK_DIR),
            # then project.yaml, then the repo-layout fallback.
            import os

            from ppsspp_dfx_mcp.config import _load_yaml_value, config_dir

            memstick_str = os.environ.get("PPSSPP_DFX_MEMSTICK_DIR", "")
            if not memstick_str:
                memstick_str = _load_yaml_value("project.yaml", "ppsspp_memstick_dir", "")
            if memstick_str:
                memstick_dir = Path(memstick_str).expanduser().resolve()
            else:
                memstick_dir = config_dir().parent.parent / "tools" / "ppsspp_dev" / "memstick"

            screenshot_dir = memstick_dir / "PSP" / "SCREENSHOT"
            if not screenshot_dir.exists():
                return b""

            existing = {f.name for f in screenshot_dir.iterdir() if f.suffix in (".png", ".jpg")}

            WM_COMMAND = 0x0111
            ID_DEBUG_TAKESCREENSHOT = 40066  # Windows/resource.h:192
            SMTO_ABORTIFHUNG = 0x0008
            user32 = ctypes.windll.user32

            def _send_take_screenshot() -> bool:
                # SendMessageW blocks until the target window's UI
                # thread processes the message — against a wedged PPSSPP
                # (modal dialog / GPU hang / exiting) it NEVER returns and
                # would freeze the entire event loop (all sessions, all
                # tools, GC). SendMessageTimeoutW + SMTO_ABORTIFHUNG bounds
                # that to 3s; to_thread keeps even the bounded block off
                # the loop. safe_screenshot is the fallback FOR a broken
                # PPSSPP, so the hung-window case is the expected one here.
                result = ctypes.c_size_t()
                ok = user32.SendMessageTimeoutW(
                    hwnd,
                    WM_COMMAND,
                    ID_DEBUG_TAKESCREENSHOT,
                    0,
                    SMTO_ABORTIFHUNG,
                    3000,
                    ctypes.byref(result),
                )
                return bool(ok)

            sent = await asyncio.to_thread(_send_take_screenshot)
            if not sent:
                logger.debug(
                    "_wm_command_screenshot: SendMessageTimeoutW timed out or "
                    "failed (target window hung?) — strategy yields no image"
                )
                return b""

            # The appearance scan and the per-file settle wait share one
            # deadline, so a PPSSPP that never finishes writing the file
            # costs at most _WM_SCREENSHOT_WAIT_S instead of blocking the
            # session (and the CPU, halted in STEPPING) forever.
            deadline = time.monotonic() + _WM_SCREENSHOT_WAIT_S
            for _ in range(100):
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(_WM_SCREENSHOT_POLL_S)
                for f in screenshot_dir.iterdir():
                    if f.suffix in (".png", ".jpg") and f.name not in existing:
                        # PPSSPP may still be writing the file when it
                        # first appears — a fixed 0.15s read returns a
                        # truncated image on slow disks, and a truncated
                        # PNG still carries a valid IHDR so downstream
                        # decoders fail with no retry. Wait for the size
                        # to stabilize across two consecutive samples,
                        # then require a complete image (IEND/EOI tail).
                        data = await _await_stable_image(f, deadline)
                        if _image_complete(data, f.suffix):
                            return data

            return b""
        except Exception as e:
            logger.debug("_wm_command_screenshot failed: %s", e)
            return b""

    async def _print_window_screenshot(self) -> bytes:
        """Screenshot via PrintWindow + PW_RENDERFULLCONTENT (FALLBACK).

        FALLBACK strategy in safe_screenshot(). Captures pixels from the
        DWM redirection surface via PrintWindow, occlusion-safe.

        Target window selection:
        - Prefer PPSSPPDisplay child window (GPU render target).
        - Fall back to PPSSPPWnd main window if child not found.

        GPU content availability depends on PPSSPP graphics backend:
        - DirectX/OpenGL: usually works.
        - Vulkan: uncertain, may capture black screen.

        GDI resource cleanup: try/finally ensures DeleteObject(hbitmap),
        DeleteDC(mem_dc), ReleaseDC(hwnd, hdc) execute on all exit paths.

        Windows-only (requires user32/gdi32). Returns b"" on non-Windows
        platforms, when PPSSPP window not found, or when PrintWindow fails.
        """
        if sys.platform != "win32":
            return b""
        if _BITMAPINFO is None or _BITMAPINFOHEADER is None:
            return b""
        try:
            import ctypes
            from ctypes import wintypes

            from PIL import Image

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32

            # 1. Find PPSSPP main window, then prefer PPSSPPDisplay child.
            hwnd_main = self._find_ppsspp_window(pid=getattr(self._client, "pid", None))
            if not hwnd_main:
                return b""

            hwnd_display = user32.FindWindowExW(hwnd_main, 0, "PPSSPPDisplay", None)
            hwnd = hwnd_display if hwnd_display else hwnd_main

            # 2. Get window dimensions.
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w <= 0 or h <= 0:
                return b""

            # 3. Set up GDI objects. Track handles for cleanup on all paths.
            hdc = 0
            mem_dc = 0
            hbitmap = 0
            old_bitmap = 0
            try:
                hdc = user32.GetWindowDC(hwnd)
                if not hdc:
                    return b""
                mem_dc = gdi32.CreateCompatibleDC(hdc)
                if not mem_dc:
                    return b""
                hbitmap = gdi32.CreateCompatibleBitmap(hdc, w, h)
                if not hbitmap:
                    return b""
                old_bitmap = gdi32.SelectObject(mem_dc, hbitmap)

                # 4. PrintWindow with PW_RENDERFULLCONTENT=2 (DWM surface).
                PW_RENDERFULLCONTENT = 2
                result = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
                if not result:
                    return b""

                # 5. Read pixels via GetDIBits (BGRA, top-down).
                bmi = _BITMAPINFO()
                bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
                bmi.bmiHeader.biWidth = w
                bmi.bmiHeader.biHeight = -h  # top-down
                bmi.bmiHeader.biPlanes = 1
                bmi.bmiHeader.biBitCount = 32
                bmi.bmiHeader.biCompression = 0  # BI_RGB

                buf_size = w * h * 4
                pixel_buf = ctypes.create_string_buffer(buf_size)
                scanlines = gdi32.GetDIBits(mem_dc, hbitmap, 0, h, pixel_buf, ctypes.byref(bmi), 0)
                if not scanlines:
                    return b""

                # 6. BGRA → RGB conversion (drop alpha channel).
                img = Image.frombytes("RGB", (w, h), pixel_buf.raw, "raw", "BGRX")
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                return buf.getvalue()
            finally:
                # 7. GDI resource cleanup on ALL exit paths.
                if mem_dc and old_bitmap:
                    gdi32.SelectObject(mem_dc, old_bitmap)
                if hbitmap:
                    gdi32.DeleteObject(hbitmap)
                if mem_dc:
                    gdi32.DeleteDC(mem_dc)
                if hdc:
                    user32.ReleaseDC(hwnd, hdc)
        except Exception as e:
            logger.warning("PrintWindow screenshot failed: %s", e, exc_info=True)
            return b""

    async def _vram_screenshot(self) -> bytes:
        """PSP VRAM direct read screenshot (LAST RESORT).

        WARNING: VRAM data is NOT synced with PPSSPP's GPU framebuffer.
        Colors will be incorrect (0% exact match, 偏蓝冷色调) — this is
        only useful for structural reference. For accurate colors, use
        `_wm_command_screenshot()` instead.

        Uses `client.read_bytes()` (NOT direct transport access) to read
        the VRAM region, then converts RGBA8888 to PNG via PIL.

        Availability assessment:
        - Scene structure recognition: partial (68.1% edge correlation).
        - Color exact verification: NOT available (0% exact match).
        - Text rendering verification: NOT available.
        - Crash diagnosis: partial (can detect black-screen / garbled).
        """
        try:
            import asyncio

            from ppsspp_dfx_mcp.config import addresses as _addresses

            W, H, STRIDE = 480, 272, 512
            BYTES_PER_PIXEL = 4  # RGBA8888
            size = STRIDE * H * BYTES_PER_PIXEL

            candidates = _addresses().get("vram_candidates", [0x04000000, 0x04088000])

            for addr in candidates:
                try:
                    # Sanctioned large read — see read_bytes docstring.
                    raw = await self._client.read_bytes(addr, size, allow_large=True)
                except Exception as e:
                    logger.debug("read_bytes at 0x%08X failed: %s", addr, e)
                    continue

                if len(raw) < size:
                    continue

                # Review-v4 W-5: the 557KB blank scan + 130k-iteration pixel
                # loop + PNG encode are pure CPU. Inline on the event loop
                # they stalled every other session (recv loops, GC, tool
                # calls) for ~1s while this path already holds the session
                # lock on a wedged PPSSPP — offload to a worker thread.
                png = await asyncio.to_thread(
                    _vram_candidate_to_png, raw, W, H, STRIDE, BYTES_PER_PIXEL
                )
                if png is not None:
                    return png

            return b""
        except Exception as e:
            logger.debug("_vram_screenshot failed: %s", e)
            return b""

    # ======================================================================
    # dump_texture (task 4.7) — with_stepping + client.texture()
    # ======================================================================

    async def dump_texture(
        self,
        level: int = 0,
    ) -> bytes:
        """Dump the currently-bound GPU texture as PNG bytes.

        PPSSPP captures the texture currently bound to the GE state — it
        does NOT support capturing by VRAM address. The `level` parameter
        selects the mipmap level. See PPSSPP `GPUBufferSubscriber.cpp:L378-
        386` (`WebSocketGPUBufferTexture` → `GPU_GetCurrentTexture`).

        Uses output_type="uri" so PPSSPP does format conversion + PNG encoding
        internally. Extracts PNG bytes from the data URI response.
        Returns b"" on any failure, bounded by ``_DUMP_TOTAL_BUDGET_S``:
        a wedged debugger (stepping preamble / GPU call / reconnect stacked
        budget) yields the same b"" failure contract, but within the
        dedicated budget instead of the old 22-32s fallback stack.
        """
        try:
            async with asyncio.timeout(_DUMP_TOTAL_BUDGET_S):
                async with self._client.with_stepping():
                    resp = await self._client.texture(level=level, output_type="uri")
        except TimeoutError as e:
            logger.warning(
                "dump_texture: no capture within the %ss dump budget "
                "(wedged debugger or stalled GPU capture): %s",
                _DUMP_TOTAL_BUDGET_S,
                e,
            )
            return b""
        except Exception as e:
            logger.debug("dump_texture failed: %s", e)
            return b""

        return _extract_png_from_data_uri(resp.get("uri", ""))

    async def dump_clut(self) -> bytes:
        """Dump the current CLUT (color look-up table) as PNG bytes.

        See PPSSPP `GPUBufferSubscriber.cpp:406-427` (`gpu.buffer.clut`).
        The CLUT is the palette 2D tiles / font glyphs index into — key
        context when debugging font rendering. Uses output_type="uri"
        (PNG encoding done by PPSSPP); the base64 pixel form has
        height==1 (single palette row).

        Returns b"" on any failure, bounded by ``_DUMP_TOTAL_BUDGET_S``
        (mirrors dump_texture).
        """
        try:
            async with asyncio.timeout(_DUMP_TOTAL_BUDGET_S):
                async with self._client.with_stepping():
                    resp = await self._client.clut(output_type="uri")
        except TimeoutError as e:
            logger.warning(
                "dump_clut: no capture within the %ss dump budget "
                "(wedged debugger or stalled GPU capture): %s",
                _DUMP_TOTAL_BUDGET_S,
                e,
            )
            return b""
        except Exception as e:
            logger.debug("dump_clut failed: %s", e)
            return b""

        return _extract_png_from_data_uri(resp.get("uri", ""))

    # ======================================================================
    # dump_buffer (task 4.8) — delegate to client.render_depth/stencil/clut
    # ======================================================================

    async def dump_buffer(
        self,
        target: Literal["depth", "stencil", "clut"],
        output_type: Literal["uri", "base64"] = "uri",
    ) -> dict[str, Any]:
        """Dump a GPU buffer by target type.

        Args:
            target: buffer type — "depth", "stencil", or "clut".
                PPSSPP captures the currently-bound buffer for each target
                (no VRAM address parameter is supported).
            output_type: output encoding ("uri" returns data URI with PNG,
                "base64" returns raw pixels). Default "uri". Applies to
                all targets including "clut".

        Returns:
            Raw response dict from the underlying GPU buffer call.
            When output_type="uri", the dict contains a "uri" field with a
            data:image/png;base64,... data URI. When output_type="base64",
            it contains "base64"/"data" fields with raw pixel data.
        """
        # Validate the target BEFORE the capture
        # and let real exceptions propagate — the previous structure caught
        # any WS failure and re-raised it as the misleading
        # "Unsupported dump_buffer target: 'depth'" ValueError.
        if target not in ("depth", "stencil", "clut"):
            raise ValueError(f"Unsupported dump_buffer target: {target!r}")
        async with self._client.with_stepping():
            if target == "depth":
                return await self._client.render_depth(output_type=output_type)
            if target == "stencil":
                return await self._client.render_stencil(output_type=output_type)
            return await self._client.clut(output_type=output_type)


# ---------- Helpers (migrated from the legacy screenshot orchestrator) ----------

_DATA_URI_PREFIX = "data:image/png;base64,"


def _extract_png_from_data_uri(uri: str) -> bytes:
    """Extract PNG bytes from a data:image/png;base64,... data URI.

    Returns b"" if uri is empty, has wrong prefix, or base64 decode fails.
    """
    if not uri or not uri.startswith(_DATA_URI_PREFIX):
        return b""
    b64 = uri[len(_DATA_URI_PREFIX) :]
    try:
        return base64.b64decode(b64)
    except Exception as e:
        logger.debug("_extract_png_from_data_uri b64decode failed: %s", e)
        return b""


async def _await_stable_image(path: Path, deadline: float) -> bytes:
    """Read ``path`` once its size has settled; return b"" if it never does.

    Bounded by ``deadline`` (a ``time.monotonic()`` value). A file that
    stays zero bytes long — PPSSPP killed mid-write, disk full, stale file
    from an earlier run — can never satisfy the settle condition, so the
    caller must not wait for it forever. The read itself is offloaded: a
    multi-megabyte PNG on a slow disk must not block the event loop.
    """
    import asyncio

    last_size = -1
    stable_samples = 0
    while stable_samples < 2:
        if time.monotonic() >= deadline:
            logger.debug("_await_stable_image: %s never settled before deadline", path)
            return b""
        await asyncio.sleep(_WM_SCREENSHOT_POLL_S)
        try:
            size = path.stat().st_size
        except OSError:
            return b""  # file vanished — let the caller's outer scan continue
        if 0 < size == last_size:
            stable_samples += 1
        else:
            stable_samples = 0
        last_size = size
    try:
        return await asyncio.to_thread(path.read_bytes)
    except OSError:
        return b""


def _image_complete(data: bytes, suffix: str) -> bool:
    """Best-effort completeness check for a freshly written screenshot file.

    A truncated PNG still carries a valid IHDR (dimensions live at the
    file head), so size-based checks alone can hand back half a file on
    a slow disk — the failure then surfaces downstream as an undecodable
    image and the WM_COMMAND strategy is never retried. Complete means:
    non-trivial size AND the format's end-of-stream marker is present
    (PNG: IEND chunk; JPEG: EOI marker).
    """
    if len(data) <= 100:
        return False
    ext = suffix.lower()
    if ext == ".png":
        # PNG file tail: <4-byte length> b"IEND" <4-byte CRC>.
        return len(data) >= 8 and data[-8:-4] == b"IEND"
    if ext in (".jpg", ".jpeg"):
        return data[-2:] == b"\xff\xd9"
    return True


def _png_dims(data: bytes) -> tuple[int, int]:
    """Extract (width, height) from PNG IHDR chunk. Returns (0, 0) on failure.

    Migrated verbatim from the legacy screenshot orchestrator so
    CaptureService can compute image dimensions without depending on
    the deleted orchestration layer.
    """
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    # IHDR width @ offset 16 (4 bytes BE), height @ offset 20 (4 bytes BE).
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return (width, height)
