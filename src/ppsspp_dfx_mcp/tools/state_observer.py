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
- Process-local singleton dict, seeded lazily from
  `.ppsspp-dfx/config/addresses.yaml` `state_probes` section on
  first access.
- `register` adds to the runtime registry (does NOT write YAML).
- `clear` empties the registry; subsequent `list` returns empty. A
  fresh process re-seeds from YAML.

Multi-session semantics:
- The registry is PROCESS-LOCAL, NOT session-isolated. Probes registered
  in one session are visible in all sessions within the same MCP server
  process. This is acceptable because probe definitions (name + address
  + size) are session-agnostic — they describe game memory layout, not
  per-session state. If two sessions target different game versions with
  different address maps, register probes per-session AND clear before
  switching sessions.

Sample-failure semantics (samples > 1):
- If ANY sample fails (e.g. transient WS error), the probe is marked
  failed with the error message; the last successfully read value is
  preserved in the `value` field for debugging. This "fail-fast but
  retain last good value" policy avoids masking errors while still
  surfacing partial data for diagnosis.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from pydantic import Field
from mcp.types import ToolAnnotations

from ppsspp_dfx_mcp import config
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.address import parse_address
from ppsspp_dfx_mcp.errors import ToolError, to_tool_error
from ppsspp_dfx_mcp.models.state_observer import (
    ObservationResult,
    ProbeObservation,
    RegisterResult,
    StateProbe,
)
from ppsspp_dfx_mcp.session.client_helper import session_client
from ppsspp_dfx_mcp.views.state_observer import StateObserverResponse
from ppsspp_dfx_mcp.server import mcp

from ppsspp_dfx_mcp.views._contract import derive_output_contract

StateObserverOutput = derive_output_contract("StateObserverOutput", StateObserverResponse)

logger = logging.getLogger(__name__)

__all__ = ["state_observer"]

_ACTIONS: tuple[str, ...] = ("register", "list", "observe", "clear")
_VALID_SIZES: tuple[int, ...] = (1, 2, 4)

# Process-local probe registry. Seeded lazily from YAML on first access.
# See module docstring "Multi-session semantics" for the sharing model.
_REGISTRY: dict[str, StateProbe] = {}
_SEEDED = False


def _seed_from_yaml() -> None:
    """Lazily seed _REGISTRY from addresses.yaml `state_probes` section.

    YAML schema:
        state_probes:
          <name>:
            address: 0x08A0D000   # required
            size: 4                 # optional, default 4
            description: "..."      # optional
    """
    global _SEEDED
    if _SEEDED:
        return
    _SEEDED = True
    try:
        addrs = config.addresses()
    except Exception as e:
        logger.warning("state_probes seed failed (addresses() error): %s", e)
        return
    probes = addrs.get("state_probes")
    if not isinstance(probes, dict):
        return
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
        _REGISTRY[str(name)] = StateProbe(
            name=str(name),
            address=addr_int,
            size=size_int,
            description=desc,
        )


def _read_method(client: Any, size: int) -> Any:
    """Pick the right read_uN method on PpssppDebugClient."""
    if size == 1:
        return client.read_u8
    if size == 2:
        return client.read_u16
    if size == 4:
        return client.read_u32
    raise ToolError(
        f"invalid size={size}; expected one of {_VALID_SIZES}", code="INTERNAL"
    )


def _resolve_target_probes(names: str) -> tuple[StateProbe, ...]:
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
    if names:
        target_names = [n.strip() for n in names.split(",") if n.strip()]
    else:
        target_names = list(_REGISTRY.keys())
    if not target_names:
        raise ToolError(
            "no probes to observe: register probes first or seed "
            "addresses.yaml `state_probes` section",
            code="INTERNAL",
        )
    missing = [n for n in target_names if n not in _REGISTRY]
    if missing:
        raise ToolError(
            f"unknown probe name(s): {missing}; "
            f"registered: {list(_REGISTRY.keys())}",
            code="INTERNAL",
        )
    return tuple(_REGISTRY[n] for n in target_names)


async def _observe_probes(
    client: Any,
    probes: tuple[StateProbe, ...],
    samples: int,
) -> ObservationResult:
    """Read current value(s) of the given probes using an existing client.

    Extracted from `state_observer` so `batch_step` can reuse it without
    opening a nested `session_client` (each session_client opens a fresh
    WS connection — see client_helper.py:14 "No client caching").

    Sample-failure policy (see module docstring "Sample-failure semantics"):
    - If ANY sample fails, the probe is marked failed (error set, value
      retains the last successfully read value for debugging).
    - Samples loop breaks on first failure (fail-fast).
    """
    observations: list[ProbeObservation] = []
    for probe in probes:
        read_fn = _read_method(client, probe.size)
        value = 0
        err = ""
        # F-04 (2026-09-08): the old `max(1, samples)` clamp was dead
        # code — every caller validates samples >= 1 up front (observe
        # rejects 0, batch_step rejects < 1, frame_snapshot passes 1).
        for _ in range(samples):
            try:
                value = await read_fn(probe.address)
            except Exception as e:
                err = str(e) or e.__class__.__name__
                # value retains the last successfully read value (or 0
                # if the first sample failed) for debugging.
                break
        observations.append(
            ProbeObservation(
                name=probe.name,
                address=probe.address,
                size=probe.size,
                value=value,
                error=err,
            )
        )
    success = sum(1 for o in observations if not o.error)
    failure = len(observations) - success
    return ObservationResult(
        action="observe",
        observations=tuple(observations),
        count=len(observations),
        success_count=success,
        failure_count=failure,
    )


# Former docstring (kept as comment; description is now the TDQS docstring):
# Aggregate state-probe registry + observer.
#
# Action → required params:
# register → session_id + name + address (+ optional size, description)
# list     → session_id
# observe  → session_id (+ optional names / samples)
# clear    → session_id
@mcp.tool(
    name="ppsspp_state_observer",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
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
                "Absolute runtime address to read, as a hex string "
                "(e.g. '0x08804000'). "
                "Required for action='register'; ignored for all other "
                "actions."
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
    
    BEHAVIOR: STATE-CHANGE. register/clear mutate the registry; observe is reliable while RUNNING. The registry is PROCESS-wide (shared across sessions), seeded from addresses.yaml state_probes, and is NOT re-seeded after clear within the same process. Delete semantics are IDEMPOTENT: clearing an unknown probe name succeeds (ok), unlike ppsspp_breakpoint mem_remove which rejects missing targets (F-5 contract, 2026-09-08).

    RETURNS: {registered|probes|observations, count, success_count, failure_count} — shape depends on the action."""
    if action not in _ACTIONS:
        raise ToolError(
            f"invalid action={action!r}; expected one of {_ACTIONS}",
            code="INTERNAL",
        )
    _seed_from_yaml()
    address_int = parse_address(address)

    logger.info(
        "tool_call",
        extra={
            "tool": "ppsspp_state_observer",
            "action": action,
            "session_id": session_id,
        },
    )

    try:
        if action == "register":
            if not name:
                raise ToolError(
                    "name is required when action=register", code="INTERNAL"
                )
            if address_int == 0:
                raise ToolError(
                    "address is required when action=register "
                    "(address=0 is NULL and not a valid probe target)",
                    code="INTERNAL",
                )
            if size not in _VALID_SIZES:
                raise ToolError(
                    f"size must be one of {_VALID_SIZES}; got {size}",
                    code="INTERNAL",
                )
            probe = StateProbe(
                name=name, address=address_int, size=size, description=description
            )
            _REGISTRY[name] = probe
            result = RegisterResult(
                action=action,
                registered=probe,
                count=len(_REGISTRY),
            )
            return StateObserverResponse.from_register(result).model_dump(
                mode="json"
            )

        if action == "list":
            probes = tuple(_REGISTRY.values())
            result = RegisterResult(
                action=action, probes=probes, count=len(probes)
            )
            return StateObserverResponse.from_list(result).model_dump(mode="json")

        if action == "clear":
            _REGISTRY.clear()
            result = RegisterResult(action=action, count=0)
            return StateObserverResponse.from_clear(result).model_dump(mode="json")

        # action == "observe"
        if samples < 1:
            raise ToolError(
                f"samples must be >= 1; got {samples}", code="INTERNAL"
            )
        target_probes = _resolve_target_probes(names)
        async with session_client(session_id) as client:
            result = await _observe_probes(client, target_probes, samples)
        return StateObserverResponse.from_observe(result).model_dump(mode="json")
    except ToolError:
        raise
    except Exception as e:
        raise to_tool_error(e) from e
