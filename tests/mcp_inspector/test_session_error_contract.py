"""End-to-end: every session-aware tool answers "which session?" identically.

FR-001 (G-1). Before the fix the surface was split: tools that resolve an
omitted ``session_id`` raised ``ARGS_INVALID`` on a zero-session server
(``resolve_session_id``'s 0-session branch used ``ArgsInvalid``), while the
schema-required tools raised ``ARGS_INVALID`` from ``require_session_id`` —
yet ``ppsspp_run_script`` already answered ``SESSION_NOT_FOUND``. A caller
could not write one branch for "no session".

This test runs over a REAL stdio JSON-RPC session against a server with a
guaranteed-EMPTY session table (``mcp_inspector_isolated`` spawns its own
subprocess + sessions.json), then calls every session-aware tool and
asserts the error code set is exactly ``{"SESSION_NOT_FOUND"}``.

"How it fails": restore either pre-fix branch (``ArgsInvalid`` in
``resolve_session_id``, or ``ArgsInvalid`` in ``require_session_id``) and
the offending tool returns ``[ARGS_INVALID]`` — the set assertion goes red.
"""

from __future__ import annotations

import pytest

_ASYNC = pytest.mark.asyncio(loop_scope="session")

#: Tools whose ``session_id`` property is NOT a plain session-consumer signal.
#: ``ppsspp_session`` manages sessions; ``ppsspp_analyze_log`` declares the
#: field but ignores it ("reserved for future use"); ``ppsspp_health``
#: reports server health and treats the id as an optional probe target.
#: ``ppsspp_run_script`` needs a session only for scripts that declare
#: ``requires_ppsspp=true`` — its SessionNotFound path is covered by
#: tests/integration/test_check_cpu_state_rewire.py and
#: test_script_exposure_sync.py::test_requires_ppsspp_without_session_raises,
#: and it cannot be driven uniformly here (the isolated server has no manifest,
#: so script lookup fails with SCRIPT_NOT_FOUND before any session logic).
_NON_SESSION_CONSUMERS = frozenset(
    {"ppsspp_session", "ppsspp_analyze_log", "ppsspp_health", "ppsspp_run_script"}
)

#: Per-tool overrides for required non-session args whose placeholder value
#: would be rejected by schema/semantic validation BEFORE the session guard
#: runs. Keys are tool names; values are extra args merged over the
#: generated placeholders.
_EXTRA_ARGS: dict[str, dict] = {
    "ppsspp_batch_step": {"steps": [{"type": "cpu_step", "mode": "into", "count": 1}]},
    "ppsspp_hold_buttons": {"buttons": "cross"},
    "ppsspp_press_button": {"button": "cross"},
    # ``observe`` is the only action that touches the session; ``list`` /
    # ``clear`` / ``register`` are local registry ops that return without one.
    "ppsspp_state_observer": {"action": "observe"},
    # The default placeholder 0x08804000 is inside the protected top.prx code
    # section, and write_memory's protection pre-flight runs before session
    # resolution; use a plain user-band address.
    "ppsspp_write_memory": {"data": "0x00000001", "address": "0x08A00000"},
}


def _placeholder(spec: dict) -> object:
    """A type-correct dummy for a required JSON-schema property."""
    if "enum" in spec:
        return spec["enum"][0]
    kind = spec.get("type")
    if kind == "integer":
        return 1
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "number":
        return 1
    return "0x08804000"


def _session_tools(tools: list) -> dict[str, dict]:
    """tool name → {args, session_required} for every session consumer."""
    out: dict[str, dict] = {}
    for tool in tools:
        schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
        props = schema.get("properties", {})
        if "session_id" not in props or tool.name in _NON_SESSION_CONSUMERS:
            continue
        required = set(schema.get("required", []))
        args: dict = {}
        for name in required:
            if name == "session_id":
                continue
            args[name] = _placeholder(props.get(name, {}))
        args.update(_EXTRA_ARGS.get(tool.name, {}))
        out[tool.name] = {"args": args, "session_required": "session_id" in required}
    return out


@_ASYNC
async def test_zero_session_error_code_is_uniform(mcp_inspector_isolated):
    """0 sessions → every session-aware tool reports SESSION_NOT_FOUND.

    Two request shapes are exercised so both resolution paths are covered:
    schema-optional tools OMIT ``session_id`` (resolver path), and
    schema-required tools pass ``session_id=""`` (guard path — the exact
    call that used to return ARGS_INVALID).
    """
    listed = await mcp_inspector_isolated.list_tools()
    session_tools = _session_tools(listed.tools)
    assert len(session_tools) >= 25, (
        f"session-tool enumeration looks wrong ({len(session_tools)} found): "
        f"{sorted(session_tools)}"
    )

    # Precondition for ppsspp_state_observer: its ``observe`` action
    # short-circuits with ARGS_INVALID when the probe library is empty
    # (before any session resolution). Register a probe so the call reaches
    # the session path under test. Registration is a local registry op and
    # needs no session.
    registered = await mcp_inspector_isolated.call_tool(
        "ppsspp_state_observer",
        {"session_id": "", "action": "register", "name": "fr001_probe", "address": "0x08A00000"},
    )
    assert not registered.is_error, f"probe registration failed: {registered.content!r}"

    import re

    observed: dict[str, str] = {}
    raw: dict[str, str] = {}
    for name, meta in sorted(session_tools.items()):
        args = dict(meta["args"])
        if meta["session_required"]:
            args["session_id"] = ""  # discriminating: pre-fix → ARGS_INVALID
        result = await mcp_inspector_isolated.call_tool(name, args)
        text = result.content[0].text if result.content else ""
        raw[name] = text
        assert result.is_error, f"{name}: expected an error on a 0-session server: {text!r}"
        # Extract the bracketed code so the failure message names the tool.
        match = re.search(r"\[([A-Z_]+)\]", text)
        observed[name] = match.group(1) if match else f"<no code: {text[:120]!r}>"

    codes = set(observed.values())
    assert codes == {"SESSION_NOT_FOUND"}, (
        "session-aware tools disagree on the no-session error code:\n"
        + "\n".join(
            f"  {n}: {c} :: {raw[n][:200]!r}"
            for n, c in sorted(observed.items())
            if c != "SESSION_NOT_FOUND"
        )
    )
