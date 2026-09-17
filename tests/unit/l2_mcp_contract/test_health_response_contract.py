"""test_health_response_contract.py — L2 MCP contract: ppsspp_health response.

Anchor: views/introspect.py `HealthResponse` + tools/introspect.py `health()`.

Contract:
- `health()` returns a dict with 8 fields: status, version,
  python_version, pydantic_version, uptime_s, tool_count, session_count,
  session_error. `session_error` is None when sessions.json is healthy,
  or a stringified error when status='degraded'.
- `tool_count` falls back to `len(_TOOL_REGISTRY)` when
  `_REGISTERED_TOOL_COUNT == 0` (e.g. before registration).
- `tool_count` equals `_REGISTERED_TOOL_COUNT` when that counter > 0.
- `HealthResponse` Pydantic model is frozen + extra='forbid'.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ppsspp_dfx_mcp import __version__
from ppsspp_dfx_mcp import server as server_mod
from ppsspp_dfx_mcp.tools.introspect import health
from ppsspp_dfx_mcp.views.introspect import HealthResponse

# ============================================================================
# health() return shape
# ============================================================================


class TestHealthReturnShape:
    """health() returns a dict matching HealthResponse.model_dump()."""

    _EXPECTED_FIELDS = {
        "status",
        "version",
        "python_version",
        "pydantic_version",
        "uptime_s",
        "tool_count",
        "session_count",
        "session_error",
    }

    @pytest.mark.asyncio
    async def test_health_returns_dict_with_expected_fields(self):
        """health() dict must contain exactly the 8 expected fields."""
        result = await health()
        assert isinstance(result, dict)
        assert set(result.keys()) == self._EXPECTED_FIELDS, f"got keys={sorted(result.keys())}"

    @pytest.mark.asyncio
    async def test_health_status_is_ok(self):
        """health()['status'] must be 'ok' (server is up if health() runs)."""
        result = await health()
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_health_version_matches_package(self):
        """health()['version'] must equal ppsspp_dfx_mcp.__version__."""
        result = await health()
        assert result["version"] == __version__


# ============================================================================
# tool_count consistency
# ============================================================================


class TestHealthToolCountConsistency:
    """health()['tool_count'] must stay consistent with the SDK registry.

    Anchor: tools/introspect.py —
        tool_count = registered_tool_count()

    The count comes from the live SDK tool registry (static decorator
    registration + dynamic script tools registered in lifespan), read
    through the single-point helper `server.registered_tool_count()`.
    """

    @pytest.mark.asyncio
    async def test_tool_count_matches_registered_count(self):
        """health()['tool_count'] equals server.registered_tool_count()."""
        result = await health()
        assert result["tool_count"] == server_mod.registered_tool_count()

    @pytest.mark.asyncio
    async def test_tool_count_reads_live_registry(self):
        """health() reads the live registry via registered_tool_count().

        Sentinel patch proves the value flows from registered_tool_count()
        (single point of registry access) into the health response.
        """
        sentinel = 999
        with patch.object(server_mod, "registered_tool_count", lambda: sentinel):
            result = await health()
        assert result["tool_count"] == sentinel

    @pytest.mark.asyncio
    async def test_tool_count_never_zero_after_registration(self):
        """After register_all_tools() runs, tool_count must be > 0.

        register_all_tools() imports every tool module (triggering the
        decorators), so the registry must be populated when tests run.
        """
        assert server_mod.registered_tool_count() > 0
        result = await health()
        assert result["tool_count"] > 0


# ============================================================================
# HealthResponse Pydantic contract
# ============================================================================


class TestHealthResponsePydanticContract:
    """HealthResponse must be frozen + extra='forbid' (FrozenModel)."""

    def test_health_response_is_frozen(self):
        """HealthResponse instances must be immutable (frozen=True)."""

        config = HealthResponse.model_config
        assert config.get("frozen") is True, (
            f"HealthResponse.model_config.frozen = {config.get('frozen')!r}"
        )

    def test_health_response_forbids_extra(self):
        """HealthResponse must reject unknown fields (extra='forbid')."""
        config = HealthResponse.model_config
        assert config.get("extra") == "forbid", (
            f"HealthResponse.model_config.extra = {config.get('extra')!r}"
        )

    def test_health_response_field_count(self):
        """HealthResponse must have exactly 8 fields (the contract surface)."""
        fields = set(HealthResponse.model_fields.keys())
        assert fields == {
            "status",
            "version",
            "python_version",
            "pydantic_version",
            "uptime_s",
            "tool_count",
            "session_count",
            "session_error",
        }
