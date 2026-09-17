"""Deterministic scoring gates for the ppsspp-dfx blind eval.

Pure module — no MCP / LLM imports, safe to unit-test standalone.

`evaluate(scenario, run, fixtures_dir)` takes a scenario card (dict, from
evals/scenarios.yaml) and a run record (dict, from the runner JSONL) and
returns per-gate results plus an overall `success` boolean. Ground-truth
values referenced by `from_fixture` specs are resolved from the recorded
fixture JSONs under tests/cassettes/fixtures, so gates never hardcode
values that drift with fixtures.

Gate types (all are "hard" gates — every applicable gate must pass):
- first_tool:    run.tool_calls[0].name in scenario.expected_first_tools
- no_tool:       zero tool calls were made
- params:        per param_expectations entries (skipped when the named
                 tool was never called — conditional expectations)
- sequence:      expected_sequence appears as a subsequence of tool names
- answer_contains: final answer contains expected values (literal or
                 fixture-resolved), mode all/any
- recovery:      when error_code appears, a corrective call must appear
                 within max_gap calls (no error = conditional pass)
- tool_used:     a call with the given name exists
- final_call_ok: the last tool call did not error
- boot_order:    with pre_state.sessions == 0, no read-class tool may be
                 called before ppsspp_session(action=start)
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any

_HEXISH_RE = re.compile(r"^(0x)?[0-9a-fA-F]+$")
_CODE_RE = re.compile(r"^\[([A-Z0-9_]+)\]")

# Tools that only make sense after a session boots (used by boot_order).
_READ_CLASS_TOOLS = {
    "ppsspp_read_memory",
    "ppsspp_disassemble",
    "ppsspp_search_disasm",
    "ppsspp_state_observer",
    "ppsspp_get_pc",
    "ppsspp_frame_snapshot",
    "ppsspp_memory_map",
    "ppsspp_search_memory_info",
    "ppsspp_query",
    "ppsspp_evaluate",
    "ppsspp_screenshot",
    "ppsspp_dump",
}


# --------------------------------------------------------------------------
# Fixture / value resolution
# --------------------------------------------------------------------------


def _load_fixture(fixtures_dir: Path, file: str) -> dict[str, Any]:
    path = fixtures_dir / file
    if not path.is_file():
        raise FileNotFoundError(f"fixture not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _dig(obj: Any, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise KeyError(f"cannot dig '{part}' in non-container at path '{dotted}'")
    return cur


def resolve_value(spec: Any, fixtures_dir: Path | None) -> Any:
    """Resolve a literal or {'from_fixture': ...} value spec."""
    if not (isinstance(spec, dict) and "from_fixture" in spec):
        return spec
    if fixtures_dir is None:
        raise ValueError("from_fixture used but fixtures_dir not provided")
    f = spec["from_fixture"]
    val = _dig(_load_fixture(fixtures_dir, f["file"]), f["path"])
    transform = f.get("transform")
    if transform == "hex":
        return f"{int(val):08X}"
    if transform == "base64_hex":
        raw = base64.b64decode(val)
        n = int(f.get("prefix_bytes", len(raw)))
        return raw[:n].hex().upper()
    if transform is None:
        return val
    raise ValueError(f"unknown transform: {transform}")


def _normalize_answer(answer: str) -> str:
    """Strip separators so 'C0 FF BD 19' and '0xC0,0xFF' both match.

    CJK characters are preserved so Chinese keyword gates work; only
    ASCII punctuation/whitespace is dropped.
    """
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", answer or "").upper()


def _match_in_answer(answer: str, expected: Any) -> bool:
    norm = _normalize_answer(answer)
    e = _normalize_answer(str(expected))
    if _HEXISH_RE.match(str(expected)):
        e = e[2:] if e.startswith("0X") else e
    return e in norm


# --------------------------------------------------------------------------
# Param expectation matching
# --------------------------------------------------------------------------


def _param_ok(args: dict[str, Any], param: str, expect: Any) -> tuple[bool, str]:
    if expect == "omitted":
        if param in args and args[param] is not None:
            return False, f"param '{param}' should be omitted, got {args[param]!r}"
        return True, "omitted as expected"
    if isinstance(expect, dict) and "exact" in expect:
        want, got = expect["exact"], args.get(param)
        if isinstance(want, str) and _HEXISH_RE.match(want) and isinstance(got, str):
            ok = got.lower() == want.lower()
        else:
            ok = got == want
        return ok, f"exact {param}={want!r}, got {got!r}"
    if isinstance(expect, dict) and "in_set" in expect:
        got = args.get(param)
        ok = got in expect["in_set"]
        return ok, f"in_set {expect['in_set']}, got {got!r}"
    if isinstance(expect, dict) and "len" in expect:
        got = args.get(param)
        ok = isinstance(got, (list, dict)) and len(got) == expect["len"]
        return (
            ok,
            f"len {expect['len']}, got {type(got).__name__}({len(got) if isinstance(got, (list, dict)) else '?'})",
        )
    raise ValueError(f"unknown expect spec: {expect!r}")


def _then_call_matches(call: dict[str, Any], then: dict[str, Any]) -> bool:
    if call.get("name") != then.get("tool"):
        return False
    args = call.get("args") or {}
    for key, want in (then.get("with_params") or {}).items():
        if want == "*any*":
            if key not in args or args[key] is None:
                return False
        elif isinstance(want, str) and _HEXISH_RE.match(want) and isinstance(args.get(key), str):
            if str(args[key]).lower() != want.lower():
                return False
        elif args.get(key) != want:
            return False
    return True


# --------------------------------------------------------------------------
# Gate implementations — each returns (passed, detail)
# --------------------------------------------------------------------------


def _real_calls(run: dict) -> list[dict[str, Any]]:
    """Tool calls excluding B2 pseudo tools (skill_read) — order gates
    measure ppsspp tool choice, not documentation reads."""
    return [c for c in (run.get("tool_calls") or []) if not c.get("pseudo")]


def _gate_first_tool(scenario: dict, run: dict) -> tuple[bool, str]:
    calls = _real_calls(run)
    expected = scenario.get("expected_first_tools") or []
    if not calls:
        return False, "no tool calls at all"
    first = calls[0].get("name")
    ok = first in expected
    return ok, f"first={first}, expected in {expected}"


def _gate_no_tool(scenario: dict, run: dict) -> tuple[bool, str]:
    calls = run.get("tool_calls") or []
    ok = not calls
    return ok, f"{len(calls)} tool calls (expected 0)"


def _gate_params(scenario: dict, run: dict) -> tuple[bool, str]:
    calls = run.get("tool_calls") or []
    failures: list[str] = []
    checked = 0
    for exp in scenario.get("param_expectations") or []:
        call = next((c for c in calls if c.get("name") == exp["tool"]), None)
        if call is None:
            continue  # conditional expectation: tool not used
        checked += 1
        ok, detail = _param_ok(call.get("args") or {}, exp["param"], exp["expect"])
        if not ok:
            failures.append(f"{exp['tool']}.{exp['param']}: {detail}")
    if failures:
        return False, "; ".join(failures)
    return True, f"{checked} expectation(s) checked (skipped when tool unused)"


def _gate_sequence(scenario: dict, run: dict) -> tuple[bool, str]:
    calls = _real_calls(run)
    expected = scenario.get("expected_sequence") or []
    names = [c.get("name") for c in calls]
    it = iter(names)
    ok = all(name in it for name in expected)
    return ok, f"expected subsequence {expected} in {names}"


def _gate_answer_contains(scenario: dict, run: dict, fixtures_dir: Path | None) -> tuple[bool, str]:
    spec = next(g for g in scenario["gates"] if g["type"] == "answer_contains")
    answer = run.get("final_answer") or ""
    mode = spec.get("mode", "all")
    results: list[str] = []
    ok_flags: list[bool] = []
    for value_spec in spec.get("values", []):
        expected = resolve_value(value_spec, fixtures_dir)
        ok = _match_in_answer(answer, expected)
        ok_flags.append(ok)
        results.append(f"{expected!r}:{'hit' if ok else 'MISS'}")
    ok = all(ok_flags) if mode == "all" else any(ok_flags)
    return ok, f"mode={mode}, " + ", ".join(results)


def _gate_recovery(scenario: dict, run: dict, spec: dict) -> tuple[bool, str]:
    calls = run.get("tool_calls") or []
    error_code = spec["error_code"]
    max_gap = int(spec.get("max_gap", 4))
    idx = next(
        (i for i, c in enumerate(calls) if c.get("error_code") == error_code),
        None,
    )
    if idx is None:
        return True, f"{error_code} not observed (conditional pass)"
    then = spec["then"]
    window = calls[idx + 1 : idx + 1 + max_gap]
    fixed = next((c for c in window if _then_call_matches(c, then)), None)
    if fixed is None:
        names = [(c.get("name"), c.get("error_code")) for c in window]
        return False, (
            f"{error_code} at call #{idx + 1} but no corrective call "
            f"{then} within {max_gap}; window={names}"
        )
    return True, f"{error_code} recovered at call #{calls.index(fixed) + 1}"


def _gate_tool_used(scenario: dict, run: dict, spec: dict) -> tuple[bool, str]:
    calls = run.get("tool_calls") or []
    n = sum(1 for c in calls if c.get("name") == spec["name"])
    ok = n >= int(spec.get("min", 1))
    return ok, f"{spec['name']} called {n} time(s), min {spec.get('min', 1)}"


def _gate_final_call_ok(scenario: dict, run: dict) -> tuple[bool, str]:
    calls = _real_calls(run)
    if not calls:
        return False, "no tool calls"
    last = calls[-1]
    ok = not last.get("is_error")
    return ok, f"last call {last.get('name')} is_error={last.get('is_error')}"


def _gate_boot_order(scenario: dict, run: dict) -> tuple[bool, str]:
    if (scenario.get("pre_state") or {}).get("sessions", 0) > 0:
        return True, "sessions pre-seeded, boot order not applicable"
    calls = _real_calls(run)
    session_idx = next(
        (i for i, c in enumerate(calls) if c.get("name") == "ppsspp_session"),
        None,
    )
    if session_idx is None:
        return False, "no ppsspp_session call before tool use"
    early = [
        (i, c.get("name"))
        for i, c in enumerate(calls[:session_idx])
        if c.get("name") in _READ_CLASS_TOOLS
    ]
    if early:
        return False, f"read-class tools called before session start: {early}"
    return True, f"session start at call #{session_idx + 1}, no early reads"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _gate_file_saved(scenario: dict, run: dict, spec: dict) -> tuple[bool, str]:
    """Real-mode structural gate: the tool call's result_preview must carry
    a `file_path` whose file EXISTS on disk and is non-empty. Stronger and
    truncation-proof versus result_field (long paths push later JSON
    fields out of the 200-char preview)."""
    calls = _real_calls(run)
    matches = [c for c in calls if c.get("name") == spec["tool"]]
    if not matches:
        return False, f"{spec['tool']} never called"
    preview = matches[-1].get("result_preview") or ""
    m = re.search(r'"file_path"\s*:\s*"([^"]+)"', preview)
    if not m:
        return False, f"no file_path in {spec['tool']} result: {preview[:80]!r}"
    # The captured group is a JSON string literal (raw server text):
    # resolve it with JSON semantics — unicode_escape would corrupt
    # multi-byte chars like the '→' in 'render→vram_fallback.png'.
    raw = m.group(1)
    try:
        path = json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        path = raw  # truncated escape at preview boundary — best effort
    p = Path(path)
    if not p.is_file():
        return False, f"saved file missing: {path}"
    size = p.stat().st_size
    ok = size > 0
    return ok, f"saved file {p.name} exists ({size} bytes)"


def _gate_result_field(scenario: dict, run: dict, spec: dict) -> tuple[bool, str]:
    """Structural real-mode gate: a tool call's result_preview must carry
    `field` with an integer value > gt (e.g. screenshot size_bytes > 0).
    Falls back to a JSON-field regex when the preview is truncated."""
    calls = _real_calls(run)
    field = spec["field"]
    gt = int(spec.get("gt", 0))
    matches = [c for c in calls if c.get("name") == spec["tool"]]
    if not matches:
        return False, f"{spec['tool']} never called"
    last = matches[-1]
    preview = last.get("result_preview") or ""
    try:
        val = json.loads(preview).get(field)
    except json.JSONDecodeError:
        m = re.search(rf'"{field}"\s*:\s*(\d+)', preview)
        val = int(m.group(1)) if m else None
    if val is None:
        return False, f"{spec['tool']} result missing numeric {field}: {preview[:80]!r}"
    ok = int(val) > gt
    return ok, f"{spec['tool']}.{field}={val} (gt {gt})"


def evaluate(
    scenario: dict[str, Any],
    run: dict[str, Any],
    fixtures_dir: Path | None = None,
) -> dict[str, Any]:
    """Score one run against one scenario. Returns {gates: [...], success}."""
    results: list[dict[str, Any]] = []
    for gate in scenario.get("gates", []):
        gtype = gate["type"]
        if gtype == "first_tool":
            ok, detail = _gate_first_tool(scenario, run)
        elif gtype == "no_tool":
            ok, detail = _gate_no_tool(scenario, run)
        elif gtype == "params":
            ok, detail = _gate_params(scenario, run)
        elif gtype == "sequence":
            ok, detail = _gate_sequence(scenario, run)
        elif gtype == "answer_contains":
            ok, detail = _gate_answer_contains(scenario, run, fixtures_dir)
        elif gtype == "recovery":
            ok, detail = _gate_recovery(scenario, run, gate)
        elif gtype == "tool_used":
            ok, detail = _gate_tool_used(scenario, run, gate)
        elif gtype == "final_call_ok":
            ok, detail = _gate_final_call_ok(scenario, run)
        elif gtype == "result_field":
            ok, detail = _gate_result_field(scenario, run, gate)
        elif gtype == "file_saved":
            ok, detail = _gate_file_saved(scenario, run, gate)
        elif gtype == "boot_order":
            ok, detail = _gate_boot_order(scenario, run)
        else:
            ok, detail = False, f"unknown gate type: {gtype}"
        results.append({"type": gtype, "pass": ok, "detail": detail})
    return {
        "gates": results,
        "success": all(r["pass"] for r in results),
    }


def extract_error_code(text: str) -> str | None:
    """Extract the leading [CODE] from a server isError text (R15 format)."""
    m = _CODE_RE.match(text or "")
    return m.group(1) if m else None
