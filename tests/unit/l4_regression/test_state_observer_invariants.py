"""L4 regression tests for state_observer tool invariants.

Locks in:
- register requires name + address (size in {1,2,4})
- list returns all registered probes
- observe requires non-empty registry (else ToolError)
- observe rejects unknown probe names
- observe rejects samples < 1
- clear empties the registry
- observe routes to read_u8/u16/u32 by probe.size

Uses monkeypatch to mock session_client. Each test clears the module-
level _REGISTRY singleton to avoid cross-test pollution.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.errors import ToolError
from ppsspp_dfx_mcp.tools import state_observer as so_module
from ppsspp_dfx_mcp.tools.state_observer import state_observer


def _reset_registry() -> None:
    """Clear the module-level registry + reset seed flag (test isolation)."""
    so_module._REGISTRY.clear()
    so_module._SEEDED = False


def _patch_client(monkeypatch: pytest.MonkeyPatch, mock: AsyncMock) -> None:
    """Patch session_client to yield mock."""

    @asynccontextmanager
    async def fake_session_client(session_id: str) -> AsyncIterator[AsyncMock]:
        yield mock

    monkeypatch.setattr(
        "ppsspp_dfx_mcp.tools.state_observer.session_client",
        fake_session_client,
    )


# ============================================================================
# register action — input validation
# ============================================================================


class TestRegisterValidation:
    """register action validates name / address / size strictly."""

    def setup_method(self):
        _reset_registry()

    @pytest.mark.asyncio
    async def test_register_requires_name(self, monkeypatch):
        mock = AsyncMock()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="name is required"):
            await state_observer(
                session_id="s", action="register", address=0x08A0D000
            )

    @pytest.mark.asyncio
    async def test_register_requires_address(self, monkeypatch):
        mock = AsyncMock()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="address is required"):
            await state_observer(
                session_id="s", action="register", name="probe1"
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_size", [0, 3, 5, 8, -1])
    async def test_register_rejects_invalid_size(
        self, monkeypatch, bad_size
    ):
        mock = AsyncMock()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="size must be one of"):
            await state_observer(
                session_id="s",
                action="register",
                name="probe1",
                address=0x08A0D000,
                size=bad_size,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("valid_size", [1, 2, 4])
    async def test_register_accepts_valid_size(
        self, monkeypatch, valid_size
    ):
        mock = AsyncMock()
        _patch_client(monkeypatch, mock)
        result = await state_observer(
            session_id="s",
            action="register",
            name=f"probe_size_{valid_size}",
            address=0x08A0D000,
            size=valid_size,
            description="test",
        )
        assert result["action"] == "register"
        assert result["registered"]["size"] == valid_size
        assert result["registered"]["name"] == f"probe_size_{valid_size}"
        assert result["count"] == 1


# ============================================================================
# list / clear actions
# ============================================================================


class TestListClear:
    """list returns all probes; clear empties the registry."""

    def setup_method(self):
        _reset_registry()

    @pytest.mark.asyncio
    async def test_list_empty_registry(self, monkeypatch):
        mock = AsyncMock()
        _patch_client(monkeypatch, mock)
        result = await state_observer(session_id="s", action="list")
        assert result["action"] == "list"
        assert result["probes"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_list_after_register(self, monkeypatch):
        mock = AsyncMock()
        _patch_client(monkeypatch, mock)
        await state_observer(
            session_id="s",
            action="register",
            name="probe_a",
            address=0x1000,
            size=4,
        )
        await state_observer(
            session_id="s",
            action="register",
            name="probe_b",
            address=0x2000,
            size=2,
        )
        result = await state_observer(session_id="s", action="list")
        assert result["count"] == 2
        names = {p["name"] for p in result["probes"]}
        assert names == {"probe_a", "probe_b"}

    @pytest.mark.asyncio
    async def test_clear_empties_registry(self, monkeypatch):
        mock = AsyncMock()
        _patch_client(monkeypatch, mock)
        await state_observer(
            session_id="s",
            action="register",
            name="probe_a",
            address=0x1000,
        )
        result = await state_observer(session_id="s", action="clear")
        assert result["action"] == "clear"
        assert result["count"] == 0
        # Verify via list.
        list_result = await state_observer(session_id="s", action="list")
        assert list_result["count"] == 0


# ============================================================================
# observe action — validation + read routing
# ============================================================================


class TestObserveValidation:
    """observe action validates registry state + name lookup."""

    def setup_method(self):
        _reset_registry()

    @pytest.mark.asyncio
    async def test_observe_empty_registry_raises(self, monkeypatch):
        mock = AsyncMock()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="no probes to observe"):
            await state_observer(session_id="s", action="observe")

    @pytest.mark.asyncio
    async def test_observe_unknown_name_raises(self, monkeypatch):
        mock = AsyncMock()
        _patch_client(monkeypatch, mock)
        await state_observer(
            session_id="s",
            action="register",
            name="known_probe",
            address=0x1000,
        )
        with pytest.raises(ToolError, match="unknown probe name"):
            await state_observer(
                session_id="s", action="observe", names="unknown"
            )

    @pytest.mark.asyncio
    async def test_observe_rejects_samples_zero(self, monkeypatch):
        mock = AsyncMock()
        _patch_client(monkeypatch, mock)
        await state_observer(
            session_id="s",
            action="register",
            name="probe1",
            address=0x1000,
        )
        with pytest.raises(ToolError, match="samples must be >= 1"):
            await state_observer(
                session_id="s", action="observe", samples=0
            )


class TestObserveReadRouting:
    """observe routes to read_u8/u16/u32 based on probe.size."""

    def setup_method(self):
        _reset_registry()

    @pytest.mark.asyncio
    async def test_observe_size_4_calls_read_u32(self, monkeypatch):
        mock = AsyncMock()
        mock.read_u32.return_value = 0xDEADBEEF
        _patch_client(monkeypatch, mock)
        await state_observer(
            session_id="s",
            action="register",
            name="probe32",
            address=0x1000,
            size=4,
        )
        result = await state_observer(
            session_id="s", action="observe", names="probe32"
        )
        mock.read_u32.assert_awaited_once_with(0x1000)
        mock.read_u8.assert_not_awaited()
        mock.read_u16.assert_not_awaited()
        obs = result["observations"][0]
        assert obs["value"] == "0xDEADBEEF"  # view layer serializes value as hex string (6bd3bff contract)
        assert obs["error"] == ""
        assert result["success_count"] == 1
        assert result["failure_count"] == 0

    @pytest.mark.asyncio
    async def test_observe_size_2_calls_read_u16(self, monkeypatch):
        mock = AsyncMock()
        mock.read_u16.return_value = 0xBEEF
        _patch_client(monkeypatch, mock)
        await state_observer(
            session_id="s",
            action="register",
            name="probe16",
            address=0x2000,
            size=2,
        )
        await state_observer(
            session_id="s", action="observe", names="probe16"
        )
        mock.read_u16.assert_awaited_once_with(0x2000)
        mock.read_u32.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_observe_size_1_calls_read_u8(self, monkeypatch):
        mock = AsyncMock()
        mock.read_u8.return_value = 0x42
        _patch_client(monkeypatch, mock)
        await state_observer(
            session_id="s",
            action="register",
            name="probe8",
            address=0x3000,
            size=1,
        )
        await state_observer(
            session_id="s", action="observe", names="probe8"
        )
        mock.read_u8.assert_awaited_once_with(0x3000)
        mock.read_u32.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_observe_all_probes_when_names_omitted(self, monkeypatch):
        mock = AsyncMock()
        mock.read_u32.return_value = 1
        mock.read_u16.return_value = 2
        _patch_client(monkeypatch, mock)
        await state_observer(
            session_id="s",
            action="register",
            name="p32",
            address=0x1000,
            size=4,
        )
        await state_observer(
            session_id="s",
            action="register",
            name="p16",
            address=0x2000,
            size=2,
        )
        result = await state_observer(session_id="s", action="observe")
        assert result["count"] == 2
        mock.read_u32.assert_awaited_once_with(0x1000)
        mock.read_u16.assert_awaited_once_with(0x2000)

    @pytest.mark.asyncio
    async def test_observe_multi_sample_calls_read_n_times(
        self, monkeypatch
    ):
        mock = AsyncMock()
        mock.read_u32.return_value = 0x1234
        _patch_client(monkeypatch, mock)
        await state_observer(
            session_id="s",
            action="register",
            name="probe1",
            address=0x1000,
        )
        await state_observer(
            session_id="s", action="observe", names="probe1", samples=3
        )
        assert mock.read_u32.await_count == 3

    @pytest.mark.asyncio
    async def test_observe_read_failure_recorded_as_error(
        self, monkeypatch
    ):
        mock = AsyncMock()
        mock.read_u32.side_effect = RuntimeError("read failed")
        _patch_client(monkeypatch, mock)
        await state_observer(
            session_id="s",
            action="register",
            name="probe1",
            address=0x1000,
        )
        result = await state_observer(
            session_id="s", action="observe", names="probe1"
        )
        obs = result["observations"][0]
        assert obs["error"] != ""
        assert obs["value"] == "0x00000000"  # hex-string contract; last good value retained on failure
        assert result["failure_count"] == 1
        assert result["success_count"] == 0


# ============================================================================
# Action validation — invalid action
# ============================================================================


class TestActionValidation:
    """Invalid action values are rejected."""

    def setup_method(self):
        _reset_registry()

    @pytest.mark.asyncio
    async def test_invalid_action_raises(self, monkeypatch):
        mock = AsyncMock()
        _patch_client(monkeypatch, mock)
        with pytest.raises(ToolError, match="invalid action"):
            await state_observer(session_id="s", action="bogus")
