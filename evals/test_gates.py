"""Unit tests for evals/gates.py — positive AND negative per gate type.

Run (from mcps/ppsspp-dfx-mcp/):
  ../.venv/ppsspp-dfx-mcp/Scripts/python.exe -m pytest evals/test_gates.py -q
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from evals.gates import evaluate, extract_error_code, resolve_value

# ---------------------------------------------------------------------------
# Fixture dir with one known fixture (tmp_path per test)
# ---------------------------------------------------------------------------

@pytest.fixture()
def fixtures_dir(tmp_path: Path) -> Path:
    d = tmp_path / "fixtures"
    d.mkdir()
    (d / "cpu.status.json").write_text(json.dumps({
        "type": "call",
        "records": [{"params": {}, "response": {"pc": 143585524}}],
    }), encoding="utf-8")
    (d / "memory.read.json").write_text(json.dumps({
        "type": "call",
        "records": [{"params": {}, "response": {
            "base64": base64.b64encode(bytes([0xC0, 0xFF, 0xBD, 0x19, 0x98])).decode()}}],
    }), encoding="utf-8")
    return d


def _run(*calls: dict, final_answer: str = "") -> dict:
    return {"tool_calls": list(calls), "final_answer": final_answer}


def _call(name: str, args: dict | None = None, *, is_error: bool = False,
          error_code: str | None = None) -> dict:
    return {"name": name, "args": args or {}, "is_error": is_error,
            "error_code": error_code}


# ---------------------------------------------------------------------------
# first_tool / no_tool
# ---------------------------------------------------------------------------

def test_first_tool_pass_and_fail():
    sc = {"expected_first_tools": ["ppsspp_get_pc"], "gates": [{"type": "first_tool"}]}
    ok = evaluate(sc, _run(_call("ppsspp_get_pc")), None)
    bad = evaluate(sc, _run(_call("ppsspp_query", {"action": "game_state"})), None)
    assert ok["success"] is True
    assert bad["success"] is False
    assert "first=" in bad["gates"][0]["detail"]


def test_first_tool_no_calls_fails():
    sc = {"expected_first_tools": ["ppsspp_get_pc"], "gates": [{"type": "first_tool"}]}
    assert evaluate(sc, _run(), None)["success"] is False


def test_no_tool_pass_and_fail():
    sc = {"gates": [{"type": "no_tool"}]}
    assert evaluate(sc, _run(), None)["success"] is True
    assert evaluate(sc, _run(_call("ppsspp_get_pc")), None)["success"] is False


# ---------------------------------------------------------------------------
# params
# ---------------------------------------------------------------------------

def test_params_omitted_exact_len():
    sc = {
        "param_expectations": [
            {"tool": "ppsspp_read_memory", "param": "session_id", "expect": "omitted"},
            {"tool": "ppsspp_read_memory", "param": "action", "expect": {"exact": "read_u32"}},
            {"tool": "ppsspp_read_memory", "param": "address", "expect": {"exact": "0x08804000"}},
            {"tool": "ppsspp_batch_step", "param": "steps", "expect": {"len": 3}},
        ],
        "gates": [{"type": "params"}],
    }
    good = _run(
        _call("ppsspp_read_memory", {"action": "read_u32", "address": "0x08804000"}),
        _call("ppsspp_batch_step", {"steps": [{}, {}, {}]}),
    )
    assert evaluate(sc, good, None)["success"] is True

    # exact fails case-sensitively equal but case-insensitively OK for hex
    hexcase = _run(_call("ppsspp_read_memory", {"action": "read_u32", "address": "0x08804000"}))
    sc_hex_only = {"param_expectations": sc["param_expectations"][:3],
                   "gates": [{"type": "params"}]}
    assert evaluate(sc_hex_only, hexcase, None)["success"] is True

    # omitted violated
    bad_omit = _run(_call("ppsspp_read_memory", {"action": "read_u32", "session_id": "abc"}))
    assert evaluate(sc_hex_only, bad_omit, None)["success"] is False

    # exact violated
    bad_exact = _run(_call("ppsspp_read_memory", {"action": "read_bytes", "address": "0x08804000"}))
    assert evaluate(sc_hex_only, bad_exact, None)["success"] is False

    # len violated
    sc_len = {"param_expectations": [sc["param_expectations"][3]], "gates": [{"type": "params"}]}
    bad_len = _run(_call("ppsspp_batch_step", {"steps": [{}, {}]}))
    assert evaluate(sc_len, bad_len, None)["success"] is False


def test_params_conditional_skip_when_tool_unused():
    sc = {
        "param_expectations": [
            {"tool": "ppsspp_query", "param": "action", "expect": {"exact": "registers"}},
        ],
        "gates": [{"type": "params"}],
    }
    # frame_snapshot chosen instead of query — expectation skipped, gate passes
    assert evaluate(sc, _run(_call("ppsspp_frame_snapshot")), None)["success"] is True


# ---------------------------------------------------------------------------
# sequence
# ---------------------------------------------------------------------------

def test_sequence_subsequence():
    sc = {"gates": [{"type": "sequence"}],
          "expected_sequence": ["ppsspp_read_memory", "ppsspp_disassemble"]}
    ok = evaluate(sc, _run(_call("ppsspp_get_pc"),
                           _call("ppsspp_read_memory"),
                           _call("ppsspp_disassemble")), None)
    bad_order = evaluate(sc, _run(_call("ppsspp_disassemble"),
                                  _call("ppsspp_read_memory")), None)
    missing = evaluate(sc, _run(_call("ppsspp_read_memory")), None)
    assert ok["success"] is True
    assert bad_order["success"] is False
    assert missing["success"] is False


# ---------------------------------------------------------------------------
# answer_contains (literal + from_fixture + normalization)
# ---------------------------------------------------------------------------

def test_answer_contains_fixture_hex_and_base64(fixtures_dir: Path):
    sc = {
        "gates": [{"type": "answer_contains", "mode": "any", "values": [
            {"from_fixture": {"file": "cpu.status.json", "path": "records.0.response.pc",
                              "transform": "hex"}},
            {"from_fixture": {"file": "memory.read.json", "path": "records.0.response.base64"}},
        ]}],
    }
    # pc=143585524 → hex 088EF0F4; agent may write 0x088EF0F4
    assert evaluate(sc, _run(final_answer="PC = 0x088EF0F4"), fixtures_dir)["success"] is True
    # or quote the base64 blob (normalized matching)
    b64 = base64.b64encode(bytes([0xC0, 0xFF, 0xBD, 0x19, 0x98])).decode()
    assert evaluate(sc, _run(final_answer=f"原始 base64: {b64}"), fixtures_dir)["success"] is True
    assert evaluate(sc, _run(final_answer="什么都没有"), fixtures_dir)["success"] is False


def test_answer_contains_all_mode_and_cjk(fixtures_dir: Path):
    sc = {
        "gates": [{"type": "answer_contains", "mode": "all", "values": [
            {"from_fixture": {"file": "cpu.status.json", "path": "records.0.response.pc",
                              "transform": "hex"}},
            "CPUCore",
        ]}],
    }
    ok = evaluate(sc, _run(final_answer="原因是 CPUCore=1，PC=0x088EF0F4"), fixtures_dir)
    miss = evaluate(sc, _run(final_answer="PC=0x088EF0F4 但没提原因"), fixtures_dir)
    assert ok["success"] is True
    assert miss["success"] is False


def test_resolve_value_hex_format(fixtures_dir: Path):
    v = resolve_value({"from_fixture": {"file": "cpu.status.json",
                                        "path": "records.0.response.pc",
                                        "transform": "hex"}}, fixtures_dir)
    assert v == "088EF0F4"
    with pytest.raises(FileNotFoundError):
        resolve_value({"from_fixture": {"file": "nope.json", "path": "x"}}, fixtures_dir)


# ---------------------------------------------------------------------------
# recovery
# ---------------------------------------------------------------------------

def test_recovery_fix_within_gap():
    sc = {"gates": [{"type": "recovery", "error_code": "PROTECTED_ADDRESS",
                     "then": {"tool": "ppsspp_write_memory", "with_params": {"force": True}},
                     "max_gap": 4}]}
    ok = evaluate(sc, _run(
        _call("ppsspp_write_memory", {"address": "0x08804500"}, is_error=True,
              error_code="PROTECTED_ADDRESS"),
        _call("ppsspp_write_memory", {"address": "0x08804500", "force": True}),
    ), None)
    assert ok["success"] is True


def test_recovery_no_fix_fails():
    sc = {"gates": [{"type": "recovery", "error_code": "PROTECTED_ADDRESS",
                     "then": {"tool": "ppsspp_write_memory", "with_params": {"force": True}},
                     "max_gap": 4}]}
    bad = evaluate(sc, _run(
        _call("ppsspp_write_memory", {"address": "0x08804500"}, is_error=True,
              error_code="PROTECTED_ADDRESS"),
        _call("ppsspp_get_pc"),
        _call("ppsspp_get_pc"),
        _call("ppsspp_get_pc"),
        _call("ppsspp_get_pc"),
    ), None)
    assert bad["success"] is False


def test_recovery_conditional_pass_and_any_param():
    sc = {"gates": [{"type": "recovery", "error_code": "CPU_NOT_STARTED",
                     "then": {"tool": "ppsspp_session", "with_params": {"action": "wait_ready"}}}]}
    # no error at all → conditional pass
    assert evaluate(sc, _run(_call("ppsspp_get_pc")), None)["success"] is True
    # *any* semantics: param present with any value
    sc2 = {"gates": [{"type": "recovery", "error_code": "SESSION_AMBIGUOUS",
                      "then": {"tool": "ppsspp_read_memory",
                               "with_params": {"session_id": "*any*"}}}]}
    ok2 = evaluate(sc2, _run(
        _call("ppsspp_read_memory", {}, is_error=True, error_code="SESSION_AMBIGUOUS"),
        _call("ppsspp_read_memory", {"session_id": "some-uuid"}),
    ), None)
    assert ok2["success"] is True


# ---------------------------------------------------------------------------
# tool_used / final_call_ok / boot_order
# ---------------------------------------------------------------------------

def test_tool_used_and_final_call_ok():
    sc = {"gates": [{"type": "tool_used", "name": "ppsspp_batch_status"}]}
    assert evaluate(sc, _run(_call("ppsspp_batch_status")), None)["success"] is True
    assert evaluate(sc, _run(_call("ppsspp_get_pc")), None)["success"] is False

    sc2 = {"gates": [{"type": "final_call_ok"}]}
    ok = evaluate(sc2, _run(_call("ppsspp_get_pc"), _call("ppsspp_read_memory")), None)
    bad = evaluate(sc2, _run(_call("ppsspp_get_pc"),
                             _call("ppsspp_read_memory", is_error=True, error_code="X")))
    assert ok["success"] is True
    assert bad["success"] is False
    assert evaluate(sc2, _run(), None)["success"] is False


def test_boot_order():
    sc = {"pre_state": {"sessions": 0}, "gates": [{"type": "boot_order"}]}
    ok = evaluate(sc, _run(
        _call("ppsspp_session", {"action": "start", "iso_path": "x.iso"}),
        _call("ppsspp_get_pc"),
    ), None)
    early_read = evaluate(sc, _run(
        _call("ppsspp_read_memory", {"address": "0x08804000"}),
        _call("ppsspp_session", {"action": "start", "iso_path": "x.iso"}),
    ), None)
    no_session = evaluate(sc, _run(_call("ppsspp_get_pc")), None)
    assert ok["success"] is True
    assert early_read["success"] is False
    assert no_session["success"] is False
    # pre-seeded → not applicable
    sc2 = {"pre_state": {"sessions": 1}, "gates": [{"type": "boot_order"}]}
    assert evaluate(sc2, _run(_call("ppsspp_get_pc")), None)["success"] is True


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------

def test_extract_error_code():
    assert extract_error_code("[PROTECTED_ADDRESS] write blocked") == "PROTECTED_ADDRESS"
    assert extract_error_code("no prefix here") is None
    assert extract_error_code("") is None


def test_unknown_gate_type_fails_closed():
    sc = {"gates": [{"type": "definitely_not_a_gate"}]}
    result = evaluate(sc, _run(), None)
    assert result["success"] is False
    assert "unknown gate type" in result["gates"][0]["detail"]


# ---------------------------------------------------------------------------
# result_field (real-mode structural gate)
# ---------------------------------------------------------------------------

def test_result_field_screenshot_nonempty():
    sc = {"gates": [{"type": "result_field", "tool": "ppsspp_screenshot",
                     "field": "size_bytes", "gt": 0}]}
    ok = evaluate(sc, {"tool_calls": [{"name": "ppsspp_screenshot", "args": {},
                                       "result_preview": '{"mode": "auto", "size_bytes": 152064, "width": 480}'}]},
                  None)
    assert ok["success"] is True


def test_result_field_zero_and_missing():
    sc = {"gates": [{"type": "result_field", "tool": "ppsspp_screenshot",
                     "field": "size_bytes", "gt": 0}]}
    zero = evaluate(sc, {"tool_calls": [{"name": "ppsspp_screenshot", "args": {},
                                         "result_preview": '{"size_bytes": 0, "empty": true}'}]}, None)
    not_called = evaluate(sc, {"tool_calls": [{"name": "ppsspp_get_pc", "args": {}}]}, None)
    truncated = evaluate(sc, {"tool_calls": [{"name": "ppsspp_screenshot", "args": {},
                                              "result_preview": '{"file_path": "", "size_bytes": 152064, "wi'}]}, None)
    assert zero["success"] is False
    assert not_called["success"] is False
    assert truncated["success"] is True  # regex fallback on truncated JSON


def test_file_saved(tmp_path):
    saved = tmp_path / "shot.png"
    saved.write_bytes(b"\x89PNG fake")
    sc = {"gates": [{"type": "file_saved", "tool": "ppsspp_screenshot"}]}
    ok = evaluate(sc, {"tool_calls": [{"name": "ppsspp_screenshot", "args": {},
                                       "result_preview": '{"file_path": "%s", "size_bytes": 9}' % str(saved).replace("\\", "\\\\")}]}, None)
    missing = evaluate(sc, {"tool_calls": [{"name": "ppsspp_screenshot", "args": {},
                                            "result_preview": '{"file_path": "%s"}' % str(tmp_path / "nope.png").replace("\\", "\\\\")}]}, None)
    nocall = evaluate(sc, {"tool_calls": [{"name": "ppsspp_get_pc", "args": {}}]}, None)
    assert ok["success"] is True
    assert missing["success"] is False
    assert nocall["success"] is False
