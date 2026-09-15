"""Smoke test view — public JSON contract for ppsspp_smoke_test."""

from __future__ import annotations

from pydantic import Field

from ppsspp_dfx_mcp.models.smoke import SmokeTestResult
from ppsspp_dfx_mcp.views._base import FrozenModel


class CheckResultView(FrozenModel):
    """Single check result entry."""

    name: str = Field(description="Check identifier (e.g. 'iso_loaded').")
    passed: bool = Field(description="True if the check succeeded.")
    detail: str = Field(default="", description="Human-readable detail.")


class SmokeTestResponse(FrozenModel):
    """Response view for ppsspp_smoke_test."""

    checks: list[CheckResultView] = Field(
        default_factory=list,
        description="Per-check results.",
    )
    overall_status: str = Field(
        default="fail",
        description="'pass' if all checks passed, else 'fail'.",
    )

    @classmethod
    def from_result(cls, result: SmokeTestResult) -> "SmokeTestResponse":
        return cls(
            checks=[
                CheckResultView(name=c.name, passed=c.passed, detail=c.detail)
                for c in result.checks
            ],
            overall_status=result.overall_status,
        )
