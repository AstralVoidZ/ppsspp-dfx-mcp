"""State observer domain models (frozen dataclass).

A state probe is a named memory address + read spec (size: u8/u16/u32)
that can be observed without stepping. Used by:
- batch_step orchestration (as a screenshot-free observation step)
- replay recording (screenshot is forbidden during recording; U2 spike)
- ad-hoc game-state inspection

Probe registry is a runtime dict (process-local singleton), seeded
from `.ppsspp-dfx/config/addresses.yaml` `state_probes` section on
first access. `register` adds to the runtime registry (does not write
the YAML file); `clear` resets the runtime registry to empty (does
NOT re-seed from YAML — use a fresh process to re-seed).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StateProbe:
    """A named memory observation probe.

    Attributes:
        name: Human-readable probe name (e.g. 'game_mode').
        address: Absolute runtime address to read (e.g. 0x08A0D000).
        size: Read width in bytes (1, 2, or 4 — maps to u8 / u16 / u32).
        description: Optional human-readable note.
    """

    name: str
    address: int
    size: int = 4
    description: str = ""


@dataclass(frozen=True)
class ProbeObservation:
    """Result of reading a single probe once.

    Attributes:
        name: Probe name that produced this observation.
        address: Address that was read.
        size: Read width used.
        value: Raw unsigned integer value read.
        error: Empty string on success; error message on failure
            (e.g. 'read_u32 failed: ...').
    """

    name: str
    address: int
    size: int
    value: int = 0
    error: str = ""


@dataclass(frozen=True)
class ObservationResult:
    """Aggregate result of an observe action.

    Attributes:
        action: Always 'observe'.
        observations: List of per-probe observations (may be empty).
        count: Number of observations (== len(observations)).
        success_count: Number of observations with error == ''.
        failure_count: Number of observations with error != ''.
    """

    action: str = "observe"
    observations: tuple[ProbeObservation, ...] = field(default_factory=tuple)
    count: int = 0
    success_count: int = 0
    failure_count: int = 0


@dataclass(frozen=True)
class RegisterResult:
    """Result of a register / list / clear action.

    Attributes:
        action: 'register' / 'list' / 'clear'.
        registered: For 'register' — the probe that was added.
        probes: For 'list' — all probes currently registered.
        count: Number of probes in the registry after the action.
    """

    action: str = "list"
    registered: StateProbe | None = None
    probes: tuple[StateProbe, ...] = field(default_factory=tuple)
    count: int = 0
    data: dict[str, Any] | None = None
