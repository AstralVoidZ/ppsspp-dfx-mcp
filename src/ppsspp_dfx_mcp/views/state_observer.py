"""State observer response views (FrozenModel)."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from ppsspp_dfx_mcp.address import format_address
from ppsspp_dfx_mcp.models.state_observer import (
    ObservationResult,
    ProbeObservation,
    RegisterResult,
    StateProbe,
)
from ppsspp_dfx_mcp.views._base import FrozenModel


class ProbeObservationView(FrozenModel):
    """Public view of a single probe observation."""

    name: str = Field(description="Probe name")
    address: str = Field(description="Address read, hex string (e.g. '0x08804000')")
    size: int = Field(description="Read width in bytes (1/2/4)")
    value: str = Field(default="0x00000000", description="Raw unsigned value read, hex string (e.g. '0x00000001')")
    error: str = Field(default="", description="Empty on success; error message on failure")

    @classmethod
    def from_result(cls, obs: ProbeObservation) -> "ProbeObservationView":
        return cls(
            name=obs.name,
            address=format_address(obs.address),
            size=obs.size,
            value=format_address(obs.value),
            error=obs.error,
        )


class StateProbeView(FrozenModel):
    """Public view of a registered probe."""

    name: str = Field(description="Probe name")
    address: str = Field(description="Absolute runtime address, hex string (e.g. '0x08804000')")
    size: int = Field(default=4, description="Read width (1/2/4)")
    description: str = Field(default="", description="Human-readable note")

    @classmethod
    def from_probe(cls, probe: StateProbe) -> "StateProbeView":
        return cls(
            name=probe.name,
            address=format_address(probe.address),
            size=probe.size,
            description=probe.description,
        )


class StateObserverResponse(FrozenModel):
    """Public response of the ppsspp_state_observer tool.

    Field presence depends on `action`:
    - register: action + registered + count
    - list: action + probes + count
    - observe: action + observations + count + success_count + failure_count
    - clear: action + count
    """

    action: str = Field(description="Observer action executed")
    registered: StateProbeView | None = Field(
        default=None, description="Probe added (action=register only)"
    )
    probes: list[StateProbeView] = Field(
        default_factory=list, description="All probes (action=list only)"
    )
    observations: list[ProbeObservationView] = Field(
        default_factory=list,
        description="Per-probe readings (action=observe only)",
    )
    count: int = Field(
        default=0,
        description="Probe count (register/list/clear) or observation count (observe)",
    )
    success_count: int = Field(
        default=0, description="Successful observations (action=observe only)"
    )
    failure_count: int = Field(
        default=0, description="Failed observations (action=observe only)"
    )
    data: dict[str, Any] | None = Field(
        default=None, description="Raw PPSSPP echo (reserved)"
    )

    @classmethod
    def from_register(cls, result: RegisterResult) -> "StateObserverResponse":
        return cls(
            action=result.action,
            registered=StateProbeView.from_probe(result.registered)
            if result.registered
            else None,
            count=result.count,
        )

    @classmethod
    def from_list(cls, result: RegisterResult) -> "StateObserverResponse":
        return cls(
            action=result.action,
            probes=[StateProbeView.from_probe(p) for p in result.probes],
            count=result.count,
        )

    @classmethod
    def from_clear(cls, result: RegisterResult) -> "StateObserverResponse":
        return cls(
            action=result.action,
            count=result.count,
        )

    @classmethod
    def from_observe(cls, result: ObservationResult) -> "StateObserverResponse":
        return cls(
            action=result.action,
            observations=[
                ProbeObservationView.from_result(o) for o in result.observations
            ],
            count=result.count,
            success_count=result.success_count,
            failure_count=result.failure_count,
        )
