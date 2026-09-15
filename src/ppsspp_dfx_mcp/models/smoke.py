"""Smoke test domain model (frozen dataclass)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CheckResult:
    """Result of a single smoke check.

    Attributes:
        name: Check identifier (e.g. 'iso_loaded').
        passed: True if the check succeeded.
        detail: Human-readable detail (error message on failure).
    """

    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class SmokeTestResult:
    """Aggregate smoke test result.

    Attributes:
        checks: Per-check results.
        overall_status: 'pass' if all checks passed, else 'fail'.
    """

    checks: list[CheckResult] = field(default_factory=list)
    overall_status: str = "fail"
