"""State-probe registry + observer — extracted from ``tools/state_observer.py`` (W19).

Holds the per-session probe registry (``_REGISTRY_BY_SESSION`` /
``_SEEDED_BY_SESSION``), the lazy YAML seeding, the name-resolution and the
merged-span observe loop. Moving the registry here removes the former
``session/ → tools/`` reverse dependency: ``session_manager`` reclaims the
side table via ``drop_session`` from this module instead of importing the
tool module.

Names and behaviour are unchanged — the tool layer imports (and re-exports)
them, and ``tools/batch_step.py`` / ``tools/workflows.py`` call
``_observe_probes`` / ``_seed_from_yaml`` / ``_resolve_target_probes`` here.

Sample-failure semantics (samples > 1):
- If ANY sample fails (e.g. transient WS error), the probe is marked
  failed with the error message; the last successfully read value is
  preserved in the ``value`` field for debugging. This "fail-fast but
  retain last good value" policy avoids masking errors while still
  surfacing partial data for diagnosis.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ppsspp_dfx_mcp import config
from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.models.state_observer import (
    ObservationResult,
    ProbeObservation,
    StateProbe,
)
from ppsspp_dfx_mcp.service.scan_engine import _merge_runs

logger = logging.getLogger(__name__)

_VALID_SIZES: tuple[int, ...] = (1, 2, 4)

# Per-session probe registries, seeded lazily from YAML on first access.
#
# Measured 2026-09-30: this used to be a single module-level dict with
# a one-shot `_SEEDED` latch. Two measured consequences:
#   1. `clear` emptied it and the latch meant the built-ins never returned --
#      for the whole server-process lifetime.
#   2. It was process-global, so a probe registered in one session was
#      visible from every other session.
# Both silently broke state_observer, batch_step(state_probe),
# frame_snapshot(probes=) and breakpoint(action='stats') at once.
#
# Probes are diagnostic state about ONE emulated session, so they are keyed
# by session id. `_SEEDED_BY_SESSION` keeps seeding idempotent per session
# without making it once-per-process.
_REGISTRY_BY_SESSION: dict[str, dict[str, StateProbe]] = {}
_SEEDED_BY_SESSION: set[str] = set()


def _registry(session_id: str) -> dict[str, StateProbe]:
    """The registry belonging to `session_id` (created on demand)."""
    return _REGISTRY_BY_SESSION.setdefault(session_id, {})


def drop_session(session_id: str) -> None:
    """Drop a stopped session's probe registry and its seed latch.

    Both containers are process-global and keyed by `uuid4()` session id, so
    without this call every session a long-lived server ever saw keeps a probe
    table and a seed marker alive forever. Mirrors
    `core/cond_filter.py:drop_session()`, which `session_manager` already
    calls at every reclamation point.
    """
    _REGISTRY_BY_SESSION.pop(session_id, None)
    _SEEDED_BY_SESSION.discard(session_id)


def _seed_from_yaml(session_id: str) -> None:
    """Lazily seed `session_id`'s registry from addresses.yaml `state_probes`.

    Idempotent per session: a second call is a no-op, and seeding
    never overwrites a probe the user registered under the same name --
    user entries are added on top, not replaced.

    YAML schema:
        state_probes:
          <name>:
            address: 0x08A0D000   # required
            size: 4                 # optional, default 4
            description: "..."      # optional
    """
    if session_id in _SEEDED_BY_SESSION:
        return
    try:
        addrs = config.addresses()
    except Exception as e:
        logger.warning("state_probes seed failed (addresses() error): %s", e)
        return
    probes = addrs.get("state_probes")
    if not isinstance(probes, dict):
        # No configured probes is a legitimate state, not a failure: mark
        # seeded so we do not re-read the file on every call.
        _SEEDED_BY_SESSION.add(session_id)
        return
    registry = _registry(session_id)
    for name, spec in probes.items():
        if not isinstance(spec, dict):
            continue
        addr = spec.get("address")
        if addr is None:
            continue
        try:
            addr_int = int(addr, 0) if isinstance(addr, str) else int(addr)
        except (TypeError, ValueError):
            continue
        size = spec.get("size", 4)
        try:
            size_int = int(size)
        except (TypeError, ValueError):
            size_int = 4
        if size_int not in _VALID_SIZES:
            size_int = 4
        desc = str(spec.get("description", ""))
        # setdefault: a user-registered probe of the same name wins, so a
        # late re-seed cannot clobber it.
        registry.setdefault(
            str(name),
            StateProbe(
                name=str(name),
                address=addr_int,
                size=size_int,
                description=desc,
            ),
        )
    # Mark seeded only on success — a transient addresses() failure
    # used to permanently disable probing for the session.
    _SEEDED_BY_SESSION.add(session_id)


def _read_method(client: Any, size: int) -> Any:
    """Pick the right read_uN method on PpssppDebugClient."""
    if size == 1:
        return client.read_u8
    if size == 2:
        return client.read_u16
    if size == 4:
        return client.read_u32
    raise ArgsInvalid(f"invalid size={size}; expected one of {_VALID_SIZES}")


def _resolve_target_probes(names: str, session_id: str) -> tuple[StateProbe, ...]:
    """Resolve a comma-separated probe name list to concrete StateProbe tuples.

    Shared by `state_observer` (action=observe) and `batch_step`'s
    state_probe step so both code paths apply identical name-parsing +
    validation rules (DRY). Empty `names` selects all registered
    probes; whitespace-only entries are discarded.

    Order of checks (matters for error messages):
    1. Resolve names → empty list means either no names given AND
       registry empty, or all names were whitespace → raise "no probes".
    2. Validate each name exists in _REGISTRY → raise "unknown probe".

    Must be called after `_seed_from_yaml()` so YAML-seeded probes are
    visible. Caller is responsible for seeding.
    """
    registry = _registry(session_id)
    if names:
        # Duplicate names produced duplicate observations and a
        # double-counted success_count — dedupe, preserving order.
        target_names = list(dict.fromkeys(n.strip() for n in names.split(",") if n.strip()))
    else:
        target_names = list(registry.keys())
    if not target_names:
        raise ArgsInvalid(
            "no probes to observe for this session: the probe library is "
            "empty. Register one with action='register', or add a "
            "`state_probes` entry to addresses.yaml. (Probes are per-session, "
            "so a probe registered under a different session_id is not "
            "visible here.)"
        )
    missing = [n for n in target_names if n not in registry]
    if missing:
        available = list(registry.keys())
        hint = (
            "the probe library for this session is empty — register one with action='register'"
            if not available
            else f"available probes: {available}"
        )
        raise ArgsInvalid(f"unknown probe name(s): {missing}; {hint}")
    return tuple(registry[n] for n in target_names)


# Absolute ceiling for ONE `observe` operation. It is the
# only bound between an unresponsive PPSSPP and a read loop that never
# returns, so both callers share this single number: the `state_observer`
# tool uses it directly, and `batch_step`'s state_probe step uses it as the
# ceiling of a budget inherited from the batch deadline (so a foreground
# batch can no longer admit steps that each outlive the promised budget).
PROBE_OBSERVE_BUDGET_S = 30.0


async def _observe_probes(
    client: Any,
    probes: tuple[StateProbe, ...],
    samples: int,
) -> ObservationResult:
    """Read current value(s) of the given probes using an existing client.

    Extracted from `state_observer` so `batch_step` can reuse it without
    opening a nested `session_client` (each session_client opens a fresh
    WS connection — see client_helper.py:14 "No client caching").

    One sample used to cost one WS round-trip PER PROBE
    (50 probes × 1400 samples ≈ 70k round-trips). Each sample now folds the
    probe addresses into merged spans (`_merge_runs`) and reads them with a
    block `read_bytes`, then extracts each probe's value by offset. Only
    single-member runs and failed/short block reads use the original
    per-point `read_u8/u16/u32` path, so error semantics match.

    Sample-failure policy (see module docstring "Sample-failure semantics"):
    - If ANY sample fails, the probe is marked failed (error set, value
      retains the last successfully read value for debugging).
    - A probe stops sampling after its first failure (fail-fast).
    """
    n = len(probes)
    values = [0] * n
    errors = [""] * n
    active = [True] * n
    # Merge window = widest probe; a wider probe may span a few narrow ones.
    merge_size = max((p.size for p in probes), default=1)

    for sample in range(samples):
        if sample:
            # Let the loop breathe between samples — back-to-back
            # awaits gave identical values, defeating the median.
            await asyncio.sleep(0.05)
        targets = sorted(
            ((probes[i].address, probes[i].size, i) for i in range(n) if active[i]),
            key=lambda t: t[0],
        )
        if not targets:
            break
        pos = 0
        for run_start, span, run_addrs in _merge_runs([t[0] for t in targets], merge_size):
            members = targets[pos : pos + len(run_addrs)]
            pos += len(run_addrs)
            if len(members) == 1:
                # Single address: keep the plain per-point read (same call
                # shape and error surface as the pre-batching implementation).
                addr, size, idx = members[0]
                try:
                    values[idx] = await _read_method(client, size)(addr)
                except Exception as e:
                    errors[idx] = str(e) or e.__class__.__name__
                    active[idx] = False
                continue
            try:
                blob = bytes(await client.read_bytes(address=run_start, size=span))
            except Exception:
                blob = b""
            if len(blob) < span:
                # Partial/unmapped span → per-point reads (fail-fast per probe).
                for addr, size, idx in members:
                    try:
                        values[idx] = await _read_method(client, size)(addr)
                    except Exception as e:
                        errors[idx] = str(e) or e.__class__.__name__
                        active[idx] = False
                continue
            for addr, size, idx in members:
                off = addr - run_start
                values[idx] = int.from_bytes(blob[off : off + size], "little")

    observations = [
        ProbeObservation(
            name=probes[i].name,
            address=probes[i].address,
            size=probes[i].size,
            value=values[i],
            error=errors[i],
        )
        for i in range(n)
    ]
    success = sum(1 for o in observations if not o.error)
    failure = len(observations) - success
    return ObservationResult(
        action="observe",
        observations=tuple(observations),
        count=len(observations),
        success_count=success,
        failure_count=failure,
    )


__all__ = [
    "PROBE_OBSERVE_BUDGET_S",
    "_observe_probes",
    "_registry",
    "_resolve_target_probes",
    "_seed_from_yaml",
    "drop_session",
]
