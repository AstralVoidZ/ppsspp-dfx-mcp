"""State observer tool wrapper.

1 tool exposed:
- ppsspp_state_observer(action, ...) — aggregate named-probe registry
  + observe (read_u8/u16/u32) without stepping

Actions (4):
- 'register' — add a probe to the runtime registry (name + address + size)
- 'list'     — list all registered probes
- 'observe'  — read current value(s) of named probe(s) or all probes
- 'clear'    — clear the runtime registry (does NOT re-seed from YAML)

Why state_probe exists (spike evidence):
- U2 (docs/experiment/experiment_ppsspp_replay_spike_v1.md §3): during
  replay recording, `gpu.buffer.screenshot` requires CPU/GPU stepping
  and is forbidden. `read_u32` works fine in RUNNING state.
- U4 (docs/experiment/experiment_state_probe_running_v1.md): RUNNING
  vs STEPPING memory reads are consistent; state_probe is trustworthy.

Probe registry:
- Keyed by session_id, seeded lazily per session from
  `.ppsspp-dfx/config/addresses.yaml` `state_probes` on first access.
- `register` adds to that session's registry (does NOT write YAML).
- `clear` removes USER-registered probes only. The YAML baseline is
  protected, so a mistaken clear can never leave a session without its
  configured probes.

Multi-session semantics:
- The registry is PER-SESSION. A probe registered under one session_id is
  invisible to every other session, and each session seeds its own
  baseline. Previously the registry was process-global, which meant one
  session could read another session's memory layout as if it were its
  own, and a single `clear` permanently emptied the shared registry for
  the whole server process (measured 2026-09-30).

Sample-failure semantics (samples > 1):
- If ANY sample fails (e.g. transient WS error), the probe is marked
  failed with the error message; the last successfully read value is
  preserved in the `value` field for debugging. This "fail-fast but
  retain last good value" policy avoids masking errors while still
  surfacing partial data for diagnosis.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace as _dc_replace
from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from ppsspp_dfx_mcp import config
from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.core.value_staleness import classify_probe_reading
from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.models.state_observer import (
    ObservationResult,
    RegisterResult,
    StateProbe,
)
from ppsspp_dfx_mcp.registry import mcp
from ppsspp_dfx_mcp.service.probe_observer import (
    _REGISTRY_BY_SESSION,  # noqa: F401 — re-exported for probe-registry tests
    _SEEDED_BY_SESSION,  # noqa: F401 — re-exported for probe-registry tests
    _VALID_SIZES,
    PROBE_OBSERVE_BUDGET_S,
    _observe_probes,
    _registry,
    _resolve_target_probes,
    _seed_from_yaml,
    drop_session,  # noqa: F401 — re-exported for probe-registry tests
)
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.spec.output_contract import derive_output_contract
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.views.state_observer import StateObserverResponse

# Static face of the derived contract(s) — mypy cannot use a dynamically
# created TypedDict as a type (see spec/output_contract.py).
if TYPE_CHECKING:
    StateObserverOutput = dict[str, Any]
else:
    StateObserverOutput = derive_output_contract("StateObserverOutput", StateObserverResponse)

logger = logging.getLogger(__name__)

__all__ = ["state_observer"]

_ACTIONS: tuple[str, ...] = ("register", "list", "observe", "clear")
# The per-session probe registry, YAML seeding, name resolution and the
# merged-span observe loop now live in `service/probe_observer.py` (W19);
# the three names above with `noqa` are re-exported so the probe-registry
# tests that reach them through this module keep working.


def _clear_probes(session_id: str, name: str | None = None) -> int:
    """Remove user-registered probes; return how many were removed.

    Probes seeded from addresses.yaml are NOT removable. They are the
    project's shared diagnostic baseline, and a single mistaken `clear`
    used to strip them for the whole process lifetime. Built-ins can
    still be overridden by `register` (which replaces the entry) when a
    project needs a different address.

    `name=None` clears every USER probe; a specific name removes just that
    one, and an unknown name is a no-op (the action is idempotent).
    """
    registry = _registry(session_id)
    if name:
        probe = registry.get(name.strip())
        if probe is None or _is_builtin(session_id, name.strip()):
            return 0
        del registry[name.strip()]
        return 1

    # Bulk clear: keep everything that came from the YAML baseline. The
    # built-in set is recomputed from the config rather than tagged on the
    # probe, so a probe registered by the user under a built-in name is
    # still protected -- the baseline is what must survive.
    builtin_names = _builtin_names(session_id)
    removed = 0
    for key in [k for k in registry if k not in builtin_names]:
        del registry[key]
        removed += 1
    return removed


def _builtin_names(session_id: str) -> set[str]:
    """Names seeded from addresses.yaml for this session."""
    try:
        addrs = config.addresses()
    except Exception:  # noqa: BLE001 — protection must not depend on config
        return set()
    probes = addrs.get("state_probes")
    if not isinstance(probes, dict):
        return set()
    return {str(n) for n, spec in probes.items() if isinstance(spec, dict)}


def _is_builtin(session_id: str, name: str) -> bool:
    return name in _builtin_names(session_id)


@mcp.tool(
    name="ppsspp_state_observer",
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
    ),
)
@translate_tool_errors
async def state_observer(
    session_id: Annotated[
        str,
        Field(description="Active session ID."),
    ],
    action: Annotated[
        Literal["register", "list", "observe", "clear"],
        Field(
            description=(
                "Observer operation. Valid values:\n"
                "- 'register': add a probe to the runtime registry "
                "(requires name + address; optional size default 4, "
                "optional description).\n"
                "- 'list': list all registered probes.\n"
                "- 'observe': read current value of named probe(s) "
                "(optional names — omit to observe all); optional "
                "samples (default 1) for multi-sample median.\n"
                "- 'clear': clear the runtime registry."
            ),
        ),
    ],
    name: Annotated[
        str,
        Field(
            default="",
            description=(
                "Probe name. Required for action='register'; optional for "
                "action='observe' (comma-separated names; omit to observe "
                "all registered probes). Ignored for list / clear."
            ),
        ),
    ] = "",
    address: Annotated[
        str,
        Field(
            default="0x0",
            description=(
                "Required for action='register'. Absolute runtime address to read, as a hex "
                "string (e.g. '0x08804000'). Not used by the other actions. The schema "
                "default of '0x0' exists for legacy callers -- do NOT rely on it when "
                "the action is 'register'."
            ),
        ),
    ] = "0x0",
    size: Annotated[
        Literal[1, 2, 4],
        Field(
            default=4,
            description=(
                "Read width in bytes (1 = u8, 2 = u16, 4 = u32). "
                "Default 4. Used by action='register'. Ignored for all "
                "other actions (probe's stored size is used at observe time)."
            ),
        ),
    ] = 4,
    description: Annotated[
        str,
        Field(
            default="",
            description="Optional human-readable note for action='register'.",
        ),
    ] = "",
    names: Annotated[
        str,
        Field(
            default="",
            description=(
                "Comma-separated probe names for action='observe'. "
                "If empty, all registered probes are observed. Ignored "
                "for all other actions."
            ),
        ),
    ] = "",
    samples: Annotated[
        int,
        Field(
            default=1,
            description=(
                "Number of samples to take per probe for action='observe' "
                "(default 1). If >1, samples are taken with a short yield "
                "between reads; the final value is the last read (caller "
                "can inspect stability by comparing samples externally)."
            ),
        ),
    ] = 1,
) -> StateObserverOutput:
    """PURPOSE: Named memory-probe registry plus running-state observation — register probes once, then sample them cheaply every loop.

    USAGE: action + session_id for observe; register needs name + address (+size 1/2/4, description); observe takes comma-separated names and samples.


    ROUTING: recurring sampled probes across loops -> here; one-shot paused snapshot -> ppsspp_frame_snapshot; single-address access watch -> ppsspp_breakpoint(action="trace").
    BEHAVIOR: STATE-CHANGE. register/clear mutate the registry; observe is reliable while RUNNING. The registry is PER-SESSION (keyed by session_id), seeded from addresses.yaml state_probes; clear removes user-registered probes only, so the configured baseline survives. Delete semantics are IDEMPOTENT: clearing an unknown probe name succeeds (ok), unlike ppsspp_breakpoint mem_remove which rejects missing targets.

    RETURNS: {registered|probes|observations, count, success_count, failure_count} — shape depends on the action."""
    if action not in _ACTIONS:
        raise ArgsInvalid(f"invalid action={action!r}; expected one of {_ACTIONS}")
    _seed_from_yaml(session_id)
    address_int = parse_address(address)

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_state_observer",
            "action": action,
            "session_id": session_id,
        },
    )

    if action == "register":
        if not name:
            raise ArgsInvalid("name is required when action=register")
        if address_int == 0:
            raise ArgsInvalid(
                "address is required when action=register "
                "(address=0 is NULL and not a valid probe target)"
            )
        if size not in _VALID_SIZES:
            raise ArgsInvalid(f"size must be one of {_VALID_SIZES}; got {size}")
        # Store under the STRIPPED name (review-v4 A-5): clear/observe
        # resolve names via strip(), so a raw " foo " key used to become a
        # zombie probe — registerable but not pointably observable/clearable.
        name = name.strip()
        probe = StateProbe(name=name, address=address_int, size=size, description=description)
        registry = _registry(session_id)
        registry[name] = probe
        result = RegisterResult(
            action=action,
            registered=probe,
            count=len(registry),
        )
        return StateObserverResponse.from_register(result).model_dump(mode="json")

    if action == "list":
        probes = tuple(_registry(session_id).values())
        result = RegisterResult(action=action, probes=probes, count=len(probes))
        return StateObserverResponse.from_list(result).model_dump(mode="json")

    if action == "clear":
        # Only user probes are removable; the addresses.yaml
        # baseline is protected so one mistaken clear cannot leave
        # the session permanently without probes.
        _clear_probes(session_id, name=name.strip() or None)
        result = RegisterResult(action=action, count=len(_registry(session_id)))
        return StateObserverResponse.from_clear(result).model_dump(mode="json")

    # action == "observe"
    if samples < 1:
        raise ArgsInvalid(f"samples must be >= 1; got {samples}")
    target_probes = _resolve_target_probes(names, session_id)
    async with session_client(session_id) as client:
        observe_result = await asyncio.wait_for(
            _observe_probes(client, target_probes, samples),
            timeout=PROBE_OBSERVE_BUDGET_S,  # 整体预算：防 PPSSPP 挂起时逐 probe 读取累积无限等待
        )
    # FR-019a (spec 008): label successful zero readings once the streak
    # reaches the threshold. The streak is per CALL, so an agent polling
    # `observe` across calls builds the history that makes the third
    # consecutive zero flag the address as suspected-stale.
    annotated = []
    for obs in observe_result.observations:
        if obs.error:
            # A failed read never reached memory: the streak must not be
            # fed, and inventing a value_status for a read that did not
            # happen would be the silent-failure pattern again.
            annotated.append(obs)
            continue
        status, note = classify_probe_reading(session_id, obs.address, obs.value)
        annotated.append(_dc_replace(obs, value_status=status, note=note))
    observe_result = ObservationResult(
        action=observe_result.action,
        observations=tuple(annotated),
        count=observe_result.count,
        success_count=observe_result.success_count,
        failure_count=observe_result.failure_count,
    )
    return StateObserverResponse.from_observe(observe_result).model_dump(mode="json")
