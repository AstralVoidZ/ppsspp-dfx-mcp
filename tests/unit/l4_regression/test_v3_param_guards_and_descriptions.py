"""A8 + W5 (review v3): int-typed params must reject booleans; the read_memory
description must stop advertising the unimplemented IR-encoding detection.

`isinstance(True, int)` is True, so `size=True` silently became a 1-byte read
and `count=True` a 1-step batch. The S11 guard is centralized in
`_common.require_int_not_bool` and applied to the sites the review listed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.errors import ArgsInvalid, StepInvalid, ToolError
from ppsspp_dfx_mcp.tools import batch_step as batch_step_mod
from ppsspp_dfx_mcp.tools import input as input_mod
from ppsspp_dfx_mcp.tools import memory as memory_mod
from ppsspp_dfx_mcp.tools import scan as scan_mod
from ppsspp_dfx_mcp.tools._common import require_int_not_bool
from ppsspp_dfx_mcp.tools.input import press_button, send_analog
from ppsspp_dfx_mcp.tools.memory import read_memory
from ppsspp_dfx_mcp.tools.scan import scan


def _patch_session_client(monkeypatch: pytest.MonkeyPatch, module) -> None:
    @asynccontextmanager
    async def fake_session_client(session_id: str | None) -> AsyncIterator[AsyncMock]:
        yield AsyncMock()

    monkeypatch.setattr(module, "session_client", fake_session_client)


# ---------------------------------------------------------------------
# require_int_not_bool itself
# ---------------------------------------------------------------------


def test_require_int_not_bool_accepts_int():
    assert require_int_not_bool(7, "n") == 7
    assert require_int_not_bool(0, "n") == 0


@pytest.mark.parametrize("bad", [True, False, "3", 1.5, None])
def test_require_int_not_bool_rejects_non_int(bad):
    with pytest.raises(ArgsInvalid):
        require_int_not_bool(bad, "n")


# ---------------------------------------------------------------------
# scan: max_results / chunk_size
# ---------------------------------------------------------------------


def _patch_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(session_id: str | None) -> str:
        return session_id or "sess"

    monkeypatch.setattr(scan_mod, "resolve_session_id", resolve)
    _patch_session_client(monkeypatch, scan_mod)


async def test_scan_max_results_rejects_bool(monkeypatch: pytest.MonkeyPatch):
    _patch_scan(monkeypatch)
    with pytest.raises(ArgsInvalid, match="max_results"):
        await scan(
            mode="pattern",
            pattern="AA",
            start_addr="0x08804000",
            end_addr="0x08805000",
            max_results=True,
        )


async def test_scan_chunk_size_rejects_bool(monkeypatch: pytest.MonkeyPatch):
    _patch_scan(monkeypatch)
    with pytest.raises(ArgsInvalid, match="chunk_size"):
        await scan(
            mode="pattern",
            pattern="AA",
            start_addr="0x08804000",
            end_addr="0x08805000",
            chunk_size=True,
        )


# ---------------------------------------------------------------------
# input: press duration / analog x+y
# ---------------------------------------------------------------------


async def test_press_button_duration_rejects_bool(monkeypatch: pytest.MonkeyPatch):
    _patch_session_client(monkeypatch, input_mod)
    with pytest.raises(ArgsInvalid, match="duration"):
        await press_button(session_id="s", button="cross", duration=True)


@pytest.mark.parametrize("kwargs", [{"x": True, "y": 128}, {"x": 128, "y": True}])
async def test_send_analog_rejects_bool(monkeypatch: pytest.MonkeyPatch, kwargs: dict):
    _patch_session_client(monkeypatch, input_mod)
    with pytest.raises(ArgsInvalid):
        await send_analog(session_id="s", **kwargs)


# ---------------------------------------------------------------------
# memory: size
# ---------------------------------------------------------------------


async def test_read_memory_size_rejects_bool(monkeypatch: pytest.MonkeyPatch):
    async def resolve(session_id: str | None) -> str:
        return session_id or "sess"

    monkeypatch.setattr(memory_mod, "resolve_session_id", resolve)
    _patch_session_client(monkeypatch, memory_mod)
    with pytest.raises(ArgsInvalid, match="size"):
        await read_memory(session_id="s", action="read_bytes", address="0x08804000", size=True)


# ---------------------------------------------------------------------
# batch_step: cpu_step count
# ---------------------------------------------------------------------


def test_batch_step_cpu_count_rejects_bool():
    with pytest.raises((StepInvalid, ToolError), match="bool"):
        batch_step_mod._validate_step({"type": "cpu_step", "mode": "into", "count": True}, 0)


# ---------------------------------------------------------------------
# W5: read_memory must not advertise IR-encoding detection
# ---------------------------------------------------------------------


def test_read_memory_description_drops_ir_encoding_claim():
    doc = read_memory.__doc__ or ""
    assert "IR_ENCODING_DETECTED" not in doc
    assert "ppsspp_disassemble" in doc
