"""Health view — public JSON contract for ppsspp_health."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from ppsspp_dfx_mcp.views._base import FrozenModel


class HealthResponse(FrozenModel):
    """Response view for ppsspp_health."""

    status: Literal["ok", "degraded"] = Field(
        description=(
            "Server status: 'ok' when all subsystems healthy; 'degraded' "
            "when sessions.json is inaccessible/corrupted but server is "
            "otherwise functional."
        ),
    )
    version: str = Field(description="MCP server version.")
    python_version: str = Field(description="Python interpreter version.")
    pydantic_version: str = Field(description="Pydantic version.")
    uptime_s: float = Field(description="Server uptime in seconds.")
    tool_count: int = Field(description="Number of registered MCP tools.")
    session_count: int = Field(
        default=0,
        description="Number of active sessions.",
    )
    session_error: str | None = Field(
        default=None,
        description=(
            "When status='degraded', describes the sessions.json issue "
            "(e.g. 'FileNotFoundError: ...' or 'JSONDecodeError: ...'). "
            "None when sessions.json is healthy."
        ),
    )
