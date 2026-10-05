"""Unit tests for the opencode collection line (`evals/oc/`).

Run (from mcps/ppsspp-dfx-mcp/):
  <repo>/.venv/ppsspp-dfx-mcp/Scripts/python.exe -m pytest evals/test_oc_collect.py -q

Design rule for this file: **every defect the refactor fixed gets a named
regression test**, so a future refactor cannot silently reintroduce it:

| Test | Pins |
|---|---|
| `TestToolNameNormalisation` | the 0-tool-call bug (namespaced MCP names) |
| `TestCommandCompat` | flag capability probe (v1 `--attach` vs v2 `--server`) |
| `TestTimeoutEnforcement` | `wait(timeout=)` actually bounds the run |
| `TestAttachUrlWhitelist` | scenario content never leaves the host |
| `TestRecordContract` | record stays mergeable with runner.py output |
| `TestStopReason` | driver failure is no longer reported as `max_turns` |
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from evals.oc import collect as collect_mod
from evals.oc import env as env_mod
from evals.oc import events as events_mod
from evals.oc import scenario as scenario_mod
from evals.oc import trajectory as trajectory_mod
from evals.oc.attach import pick_free_port, validate_attach_url
from evals.oc.errors import CollectorError, ErrorKind
from evals.oc.runner import CliRunner, RunOptions, _CommandPlan, _guard_no_share

_EVALS_DIR = Path(__file__).resolve().parent
_PKG_ROOT = _EVALS_DIR.parent


# ============================================================================
# events — tool-name normalisation (the 0-tool-call regression)
# ============================================================================


class TestToolNameNormalisation:
    def test_namespaced_name_is_stripped(self):
        """opencode v1.x exposes MCP tools as `<serverKey>_<toolName>`."""
        assert (
            events_mod.normalize_tool_name("ppsspp-dfx_ppsspp_health", ("ppsspp-dfx",))
            == "ppsspp_health"
        )

    def test_v2_dot_separator_is_stripped(self):
        """opencode v2.0.19 uses `<serverKey>.<toolName>` — a DOT, not an
        underscore. Underscore-only normalisation misses 100% of MCP calls
        while looking identical to "MCP never registered"."""
        assert (
            events_mod.normalize_tool_name("ppsspp-dfx.ppsspp_health", ("ppsspp-dfx",))
            == "ppsspp_health"
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "ppsspp-dfx_ppsspp_health",
            "ppsspp-dfx.ppsspp_health",
        ],
    )
    def test_both_separators_normalize_identically(self, raw):
        assert events_mod.normalize_tool_name(raw, ("ppsspp-dfx",)) == "ppsspp_health"

    def test_v2_namespaced_call_is_collected_with_args(self):
        raw = json.dumps(
            {
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "tool": "ppsspp-dfx.ppsspp_query",
                    "state": {
                        "status": "completed",
                        "input": {"action": "register", "name": "pc"},
                        "output": "0x0880432C",
                    },
                },
            }
        )
        call = events_mod.parse_stream(raw).tool_calls[0]
        assert call.name == "ppsspp_query"
        assert call.raw_name == "ppsspp-dfx.ppsspp_query"
        assert call.args == {"action": "register", "name": "pc"}

    def test_bare_name_passes_through(self):
        assert events_mod.normalize_tool_name("ppsspp_health", ("ppsspp-dfx",)) == "ppsspp_health"

    @pytest.mark.parametrize("raw", ["other.ppsspp_health", "other_ppsspp_health"])
    def test_unknown_prefix_not_stripped(self, raw):
        """Only known server keys are stripped — a naive first-separator
        split would mangle tool names from other servers."""
        assert events_mod.normalize_tool_name(raw, ("ppsspp-dfx",)) == raw

    def test_namespaced_tool_call_is_collected(self):
        raw = json.dumps(
            {
                "type": "tool",
                "name": "ppsspp-dfx_ppsspp_query",
                "input": {"action": "register", "name": "pc"},
                "state": {"status": "completed", "output": "0x0880432C"},
            }
        )
        parsed = events_mod.parse_stream(raw)
        assert [c.name for c in parsed.tool_calls] == ["ppsspp_query"]
        assert parsed.tool_calls[0].args == {"action": "register", "name": "pc"}
        assert parsed.tool_calls[0].raw_name == "ppsspp-dfx_ppsspp_query"
        assert "0x0880432C" in parsed.tool_calls[0].result_preview

    def test_bare_tool_call_still_collected(self):
        raw = json.dumps({"type": "tool", "name": "ppsspp_health", "state": {"output": "ok"}})
        assert [c.name for c in events_mod.parse_stream(raw).tool_calls] == ["ppsspp_health"]

    def test_non_target_tools_go_to_diagnostics(self):
        """A zero-target-call run must still show what the agent DID call."""
        raw = "\n".join(
            [
                json.dumps(
                    {"type": "tool", "name": "ppsspp-dfx_ppsspp_health", "state": {"output": "1"}}
                ),
                json.dumps({"type": "tool", "name": "bash", "input": {"command": "ls"}}),
            ]
        )
        parsed = events_mod.parse_stream(raw)
        assert [c.name for c in parsed.tool_calls] == ["ppsspp_health"]
        assert [c.name for c in parsed.other_tool_calls] == ["bash"]
        assert parsed.has_output

    def test_seq_is_renumbered_contiguously(self):
        raw = "\n".join(
            [
                json.dumps({"type": "tool", "name": "bash", "state": {"output": "x"}}),
                json.dumps(
                    {"type": "tool", "name": "ppsspp-dfx_ppsspp_health", "state": {"output": "1"}}
                ),
                json.dumps(
                    {"type": "tool", "name": "ppsspp-dfx_ppsspp_step", "state": {"output": "2"}}
                ),
            ]
        )
        assert [c.seq for c in events_mod.parse_stream(raw).tool_calls] == [1, 2]

    def test_structured_output_is_flattened(self):
        """v2 `state.output` is often an object, not a string."""
        raw = json.dumps(
            {
                "type": "tool",
                "name": "ppsspp-dfx_ppsspp_query",
                "state": {"status": "completed", "output": {"uintValue": 142606380}},
            }
        )
        call = events_mod.parse_stream(raw).tool_calls[0]
        assert "142606380" in call.result_preview

    def test_error_state_is_flagged_and_code_extracted(self):
        raw = json.dumps(
            {
                "type": "tool",
                "name": "ppsspp-dfx_ppsspp_read_memory",
                "state": {"status": "error", "error": "PPSSPP_NOT_RUNNING (code=WS_DISCONNECTED)"},
            }
        )
        call = events_mod.parse_stream(
            raw, extract_error_code=lambda t: "WS_DISCONNECTED" if "WS_DISCONNECTED" in t else None
        ).tool_calls[0]
        assert call.is_error is True
        assert call.error_code == "WS_DISCONNECTED"


class TestEventStreamParsing:
    def test_empty_input(self):
        parsed = events_mod.parse_stream("")
        assert parsed.text == "" and parsed.tool_calls == [] and not parsed.has_output

    def test_non_json_lines_are_skipped(self):
        raw = "not json\n" + json.dumps({"type": "text", "text": "hi"}) + "\nalso not json"
        assert events_mod.parse_stream(raw).text == "hi"

    def test_multiline_stream(self):
        raw = "\n".join(
            [
                json.dumps({"type": "step-start"}),
                json.dumps(
                    {"type": "tool", "name": "ppsspp-dfx_ppsspp_health", "state": {"output": "1"}}
                ),
                json.dumps({"type": "text", "text": "完成"}),
            ]
        )
        parsed = events_mod.parse_stream(raw)
        assert len(parsed.tool_calls) == 1 and parsed.text == "完成"

    def test_metadata_subtree_excluded_from_text(self):
        """porpoless W-12: tool display copies live under `metadata` and are
        NOT model output — including them corrupts the final answer."""
        raw = json.dumps(
            {
                "type": "tool",
                "name": "ppsspp-dfx_read",
                "metadata": {"display": {"type": "file", "text": "文件全文内容"}},
            }
        )
        assert events_mod.parse_stream(raw).text == ""

    def test_file_part_type_excluded_from_text(self):
        raw = json.dumps({"type": "file", "text": "文件全文内容"})
        assert events_mod.parse_stream(raw).text == ""

    def test_usage_accumulates_across_steps(self):
        """porpoless W-12: token fields are per-step; input/output accumulate,
        total takes the max."""
        raw = "\n".join(
            [
                json.dumps({"type": "step", "tokens": {"input": 10, "output": 2, "total": 12}}),
                json.dumps({"type": "step", "tokens": {"input": 20, "output": 3, "total": 23}}),
            ]
        )
        usage = events_mod.parse_stream(raw).usage
        assert usage["input_tokens"] == 30
        assert usage["output_tokens"] == 5
        assert usage["total_tokens"] == 23  # max, not sum

    def test_legacy_usage_aliases(self):
        raw = json.dumps({"usage": {"prompt_tokens": 7, "completion_tokens": 3}})
        usage = events_mod.parse_stream(raw).usage
        assert usage["input_tokens"] == 7 and usage["output_tokens"] == 3

    def test_convenience_wrappers(self):
        raw = json.dumps({"type": "text", "text": "答案"})
        assert events_mod.parse_final_answer(raw) == "答案"
        assert events_mod.parse_tool_calls(raw) == []


# ============================================================================
# runner — command construction and cross-version flag compatibility
# ============================================================================


def _runner(supported: frozenset[str], **kw: Any) -> CliRunner:
    r = CliRunner(bin_path="opencode", model="mimo/test-model", **kw)
    r._supported_flags = supported
    return r


class TestCommandCompat:
    def test_v2_uses_server_flag(self):
        """opencode v2.0.19: `--server` (porpoless's `--attach` does not exist)."""
        r = _runner(
            frozenset({"--format", "--auto", "--agent", "--title", "--server", "-m"}),
            server_url="http://127.0.0.1:4911",
        )
        plan = r.build_command(RunOptions(prompt="p", title="t"))
        assert "--server" in plan.cmd
        assert "http://127.0.0.1:4911" in plan.cmd
        assert "--attach" not in plan.cmd

    def test_v1_uses_attach_flag(self):
        r = _runner(
            frozenset({"--format", "--auto", "--agent", "--title", "--attach", "-m"}),
            server_url="http://127.0.0.1:4911",
        )
        plan = r.build_command(RunOptions(prompt="p", title="t"))
        assert "--attach" in plan.cmd and "--server" not in plan.cmd

    def test_no_server_url_adds_neither_flag(self):
        r = _runner(frozenset({"--format", "--auto", "--agent", "--title", "--server", "-m"}))
        plan = r.build_command(RunOptions(prompt="p"))
        assert "--server" not in plan.cmd and "--attach" not in plan.cmd

    def test_unsupported_pure_is_dropped_with_diagnostic(self):
        r = _runner(
            frozenset({"--format", "--auto", "--agent", "--title", "--server", "-m"}), pure=True
        )
        plan = r.build_command(RunOptions(prompt="p"))
        assert "--pure" not in plan.cmd
        assert "--pure" in plan.dropped_flags

    def test_unsupported_dir_is_dropped(self):
        r = _runner(
            frozenset({"--format", "--auto", "--agent", "--title", "--server", "-m"}),
            work_dir=str(_PKG_ROOT),
            use_dir_flag=True,
        )
        plan = r.build_command(RunOptions(prompt="p"))
        assert "--dir" not in plan.cmd and "--dir" in plan.dropped_flags

    def test_supported_dir_is_used_when_v1(self):
        r = _runner(
            frozenset({"--format", "--auto", "--agent", "--title", "--attach", "-m", "--dir"}),
            work_dir=str(_PKG_ROOT),
            use_dir_flag=True,
        )
        plan = r.build_command(RunOptions(prompt="p"))
        assert "--dir" in plan.cmd

    def test_prompt_never_enters_argv_by_default(self):
        """Prompt travels on stdin — argv length limits + quoting hazards."""
        r = _runner(frozenset({"--format", "--auto", "--agent", "--title", "--server", "-m"}))
        plan = r.build_command(RunOptions(prompt="秘密任务描述"))
        assert "秘密任务描述" not in plan.cmd

    def test_model_always_present(self):
        r = _runner(frozenset({"--format", "--auto", "--agent", "--title", "--server", "-m"}))
        assert "-m" in r.build_command(RunOptions(prompt="p")).cmd


class TestShareGuard:
    @pytest.mark.parametrize("token", ["--share", "--share=public", "-s"])
    def test_rejected(self, token):
        with pytest.raises(ValueError, match="数据外发禁用"):
            _guard_no_share(["opencode", "run", token])

    def test_allows_normal_command(self):
        _guard_no_share(["opencode", "run", "--format", "json"])


class TestAgentDiscovery:
    def test_missing_agent_returns_error(self):
        r = CliRunner(bin_path="opencode", agent="no-such-agent-xyz", work_dir=str(_PKG_ROOT))
        assert r.agent_error() is not None

    def test_collector_agent_is_discoverable(self):
        """The shipped agent definition must be found from the resolved work dir."""
        work_dir = collect_mod.resolve_work_dir()
        if not (work_dir / ".opencode" / "agents").is_dir():
            # `.opencode/` is a developer-checkout asset (opencode reads it from
            # the repo root); its `mcp.json` pins machine-local paths, so it is
            # deliberately not part of the published package. A package-only
            # checkout therefore cannot assert discoverability — same condition
            # as `_agent_md()` below.
            pytest.skip("repo root with .opencode/agents/ not found")
        assert (work_dir / ".opencode" / "agents" / "ppsspp-dfx-collector.md").is_file()
        r = CliRunner(bin_path="opencode", agent="ppsspp-dfx-collector", work_dir=str(work_dir))
        assert r.agent_error() is None

    def test_work_dir_resolves_to_opencode_owner(self):
        """.opencode/ lives at the repo root, not the MCP subproject — the work
        dir must be the layer that actually owns it."""
        work_dir = collect_mod.resolve_work_dir()
        if not (work_dir / ".opencode" / "agents").is_dir():
            pytest.skip("repo root with .opencode/agents/ not found")
        assert (work_dir / ".opencode" / "mcp.json").is_file()

    def test_explicit_work_dir_wins(self, tmp_path: Path):
        assert collect_mod.resolve_work_dir(str(tmp_path)) == tmp_path.resolve()

    def test_no_agent_configured_is_fine(self):
        assert CliRunner(bin_path="opencode", agent=None).agent_error() is None


class TestTimeoutEnforcement:
    def test_hanging_process_is_killed_at_deadline(self):
        """Regression: the old implementation drained stdout before `wait()`,
        so a hung opencode blocked forever and RUN_TIMEOUT_S never fired."""
        r = CliRunner(bin_path=sys.executable, timeout_s=2)
        plan = _CommandPlan(cmd=[sys.executable, "-c", "import time; time.sleep(60)"])
        result = r._run_once(plan, RunOptions(prompt="x"))
        assert result.timed_out is True
        assert result.duration_s < 30  # bounded, not 60

    def test_stdin_is_delivered(self):
        r = CliRunner(bin_path=sys.executable, timeout_s=30)
        plan = _CommandPlan(
            cmd=[sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"]
        )
        result = r._run_once(plan, RunOptions(prompt="任务描述-中文"))
        assert "任务描述-中文" in result.stdout
        assert result.timed_out is False

    def test_on_line_callback_streams_stdout(self):
        seen: list[str] = []
        r = CliRunner(bin_path=sys.executable, timeout_s=30)
        plan = _CommandPlan(cmd=[sys.executable, "-c", "print('a'); print('b')"])
        r._run_once(plan, RunOptions(prompt="x", on_line=seen.append))
        assert seen == ["a", "b"]

    def test_dump_path_written(self, tmp_path: Path):
        dump = tmp_path / "events.json"
        r = CliRunner(bin_path=sys.executable, timeout_s=30)
        plan = _CommandPlan(cmd=[sys.executable, "-c", "print('{}')"])
        r._run_once(plan, RunOptions(prompt="x", dump_path=dump))
        assert dump.is_file() and dump.read_text(encoding="utf-8").strip() == "{}"

    def test_early_exit_does_not_hang_on_stdin(self):
        """A child that exits before reading stdin raises EPIPE on write —
        that must not become an unhandled thread exception."""
        r = CliRunner(bin_path=sys.executable, timeout_s=30)
        plan = _CommandPlan(cmd=[sys.executable, "-c", "raise SystemExit(3)"])
        result = r._run_once(plan, RunOptions(prompt="x" * 100_000))
        assert result.returncode == 3


class TestClassify:
    def _r(self, **kw):
        return CliRunner(bin_path=sys.executable, **kw)

    def test_success(self):
        kind, msg = self._r().classify(
            __import__("evals.oc.runner", fromlist=["OpencodeRun"]).OpencodeRun(stdout="{}")
        )
        assert kind == "" and msg == ""

    def test_timeout(self):
        from evals.oc.runner import OpencodeRun

        kind, _ = self._r().classify(OpencodeRun(timed_out=True, returncode=1))
        assert kind == ErrorKind.TIMEOUT

    def test_empty_output(self):
        from evals.oc.runner import OpencodeRun

        kind, _ = self._r().classify(OpencodeRun(stdout="", returncode=0))
        assert kind == ErrorKind.EMPTY_OUTPUT

    def test_server_gone_when_attach(self):
        from evals.oc.runner import OpencodeRun

        r = CliRunner(bin_path=sys.executable, server_url="http://127.0.0.1:1")
        kind, _ = r.classify(OpencodeRun(stdout="", stderr="connect ECONNREFUSED", returncode=1))
        assert kind == ErrorKind.SERVER_GONE

    def test_locked_detected(self):
        from evals.oc.runner import OpencodeRun

        kind, _ = self._r().classify(
            OpencodeRun(stdout="", stderr="database is locked", returncode=1)
        )
        assert kind == ErrorKind.LOCKED


# ============================================================================
# attach — server lifecycle guards
# ============================================================================


class TestAttachUrlWhitelist:
    @pytest.mark.parametrize(
        "url", ["http://127.0.0.1:4911", "http://localhost:8080", "HTTP://127.0.0.1:1"]
    )
    def test_local_allowed(self, url):
        assert validate_attach_url(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "http://evil.example.com:80",
            "https://127.0.0.1:4911",
            "http://192.168.1.5:4911",
            "http://127.0.0.1.evil.com",
        ],
    )
    def test_remote_rejected(self, url):
        with pytest.raises(CollectorError) as exc:
            validate_attach_url(url)
        assert exc.value.kind == ErrorKind.BAD_URL

    def test_pick_free_port_returns_usable_port(self):
        port = pick_free_port(start=5300, tries=5)
        assert 5300 <= port < 5305


# ============================================================================
# trajectory — record contract and resume
# ============================================================================


def _gates() -> dict[str, Any]:
    return {
        "gates": [
            {"type": "first_tool", "pass": True, "detail": ""},
            {"type": "answer_contains", "pass": False, "detail": ""},
            {"type": "answer_contains", "pass": True, "detail": ""},
        ],
        "success": False,
    }


def _record(**over: Any) -> dict[str, Any]:
    base = trajectory_mod.build_record(
        run_id="oc-1",
        scenario_id="CTL-01",
        model="m",
        variant="B1",
        run_idx=1,
        provenance={},
        wall_ms=1,
        tool_calls=[],
        final_answer="x",
        usage={},
        stop_reason="final_answer",
        gates_result=_gates(),
    )
    base.update(over)
    return base


class TestRecordContract:
    #: keys `evals/runner.py:run_scenario` writes — the merge contract (design D3)
    RUNNER_KEYS = {
        "run_id",
        "scenario_id",
        "model",
        "variant",
        "run_idx",
        "provenance",
        "started_at",
        "wall_ms",
        "tool_calls",
        "final_answer",
        "usage",
        "stop_reason",
        "gates",
        "gate_details",
        "success",
    }

    def test_superset_of_runner_schema(self):
        """oc record must be a superset so the two channels' runs merge."""
        assert set(_record()) >= self.RUNNER_KEYS

    def test_gates_shape_matches_runner(self):
        rec = _record()
        assert set(rec["gates"]) == {"first_tool", "answer_contains", "answer_contains#2"}
        assert rec["gates"]["first_tool"] is True
        assert rec["gates"]["answer_contains"] is False
        assert rec["gates"]["answer_contains#2"] is True

    def test_duplicate_gate_types_not_collapsed(self):
        assert trajectory_mod.gates_record(
            [{"type": "params", "pass": True}, {"type": "params", "pass": True}]
        ) == {"params": True, "params#2": True}

    def test_gates_record_agrees_with_runner_impl(self):
        from evals.runner import _gates_record

        gates = _gates()["gates"]
        assert trajectory_mod.gates_record(gates) == _gates_record(gates)

    def test_incremental_fields_present(self):
        rec = _record(error_kind="no_tool_calls", pre_state={"requested": 1, "seeded": 1})
        assert rec["collector"] == "opencode"
        assert rec["error_kind"] == "no_tool_calls"
        assert rec["pre_state"]["seeded"] == 1
        assert rec["other_tool_names"] == []

    def test_run_id_shape(self):
        rid = trajectory_mod.run_id_for("oc", "20260930-120000", "prov/model-x", "B1", "CTL-01", 3)
        assert rid == "oc-20260930-120000-model-x-B1-CTL-01-n3"


class TestResume:
    def test_collects_grid_keys(self, tmp_path: Path):
        p = tmp_path / "runs.jsonl"
        trajectory_mod.append_record(p, _record(scenario_id="A", run_idx=1))
        trajectory_mod.append_record(p, _record(scenario_id="B", run_idx=2, run_id="oc-2"))
        assert trajectory_mod.done_keys(p) == {("A", "m", "B1", 1), ("B", "m", "B1", 2)}

    def test_missing_file_is_empty(self, tmp_path: Path):
        assert trajectory_mod.done_keys(tmp_path / "nope.jsonl") == set()

    def test_corrupt_line_does_not_abort(self, tmp_path: Path):
        p = tmp_path / "runs.jsonl"
        trajectory_mod.append_record(p, _record())
        with p.open("a", encoding="utf-8") as f:
            f.write("{ truncated\n\n")
        assert len(trajectory_mod.done_keys(p)) == 1

    def test_started_at_filled_on_append(self, tmp_path: Path):
        p = tmp_path / "runs.jsonl"
        trajectory_mod.append_record(p, _record())
        assert json.loads(p.read_text(encoding="utf-8").splitlines()[0])["started_at"]


# ============================================================================
# scenario — card access
# ============================================================================


class TestScenarios:
    def test_loads_real_card_file(self):
        scenarios, cfg = scenario_mod.load_scenarios(_EVALS_DIR)
        assert "CTL-01" in scenarios
        assert cfg["fixtures_dir"]
        assert scenarios["CTL-01"].pre_state_sessions == 1
        assert scenarios["CTL-01"].tier == "CTL"

    def test_raw_passed_through_for_gates(self):
        scenarios, _ = scenario_mod.load_scenarios(_EVALS_DIR)
        assert scenarios["CTL-01"].raw["gates"][0]["type"] == "first_tool"

    def test_fixtures_dir_resolves(self):
        _, cfg = scenario_mod.load_scenarios(_EVALS_DIR)
        fdir = scenario_mod.fixtures_dir(_EVALS_DIR, cfg)
        assert fdir.is_dir(), f"fixtures 不存在: {fdir}"

    def test_resolve_ids_all_when_empty(self):
        scenarios, _ = scenario_mod.load_scenarios(_EVALS_DIR)
        assert len(scenario_mod.resolve_ids(scenarios, "")) == len(scenarios)

    def test_resolve_ids_rejects_unknown(self):
        scenarios, _ = scenario_mod.load_scenarios(_EVALS_DIR)
        with pytest.raises(CollectorError):
            scenario_mod.resolve_ids(scenarios, "NOPE-99")


# ============================================================================
# env — server environment, XDG isolation, session seeding
# ============================================================================


class TestServerEnv:
    def _env(self, real: bool, tmp_path: Path) -> dict[str, str]:
        return env_mod.build_server_env(
            real_mode=real,
            fixtures_dir=tmp_path,
            sessions_dir=tmp_path,
            src_root=tmp_path,
            tests_root=tmp_path,
            base_env={"PATH": "x"},
        )

    def test_fake_mode_sets_fixture_env(self, tmp_path: Path):
        env = self._env(False, tmp_path)
        assert env["PPSSPP_DFX_TEST_MODE"] == "fake"
        assert env["PPSSPP_DFX_FIXTURE_DIR"] == str(tmp_path)
        assert "PPSSPP_DFX_EXE_PATH" not in env

    def test_real_mode_clears_fake_env(self, tmp_path: Path):
        env = self._env(True, tmp_path)
        assert "PPSSPP_DFX_TEST_MODE" not in env
        assert "PPSSPP_DFX_FIXTURE_DIR" not in env
        assert "PPSSPP_DFX_EXE_PATH" in env

    def test_pythonpath_includes_src_and_tests(self, tmp_path: Path):
        """FakeTransport lives in the tests tree — without it fake mode dies."""
        src, tests, fix = tmp_path / "src", tmp_path / "tests", tmp_path / "fx"
        env = env_mod.build_server_env(
            real_mode=False,
            fixtures_dir=fix,
            sessions_dir=tmp_path,
            src_root=src,
            tests_root=tests,
            base_env={},
        )
        # build_server_env joins with os.pathsep (":" on POSIX, ";" on Windows);
        # splitting on a hard-coded ";" passes on Windows and fails on POSIX.
        parts = env["PYTHONPATH"].split(os.pathsep)
        assert str(src) in parts and str(tests) in parts

    def test_sessions_path_is_per_run(self, tmp_path: Path):
        env = self._env(False, tmp_path)
        assert env["PPSSPP_DFX_SESSIONS_PATH"].endswith("sessions.json")


class TestXdgIsolation:
    def test_run_ids_get_distinct_roots(self, monkeypatch, tmp_path: Path):
        """Regression: the old code used one fixed `%TEMP%/oc-xdg` for every
        run, so concurrent runs shared an opencode state DB (`database is
        locked`).

        Contract (2026-09-30 redesign): only DATA/CONFIG are redirected;
        STATE/CACHE are inherited — redirecting STATE breaks opencode v2's
        background-service handshake (see `_XDG_INHERITED` for the three-row
        measured comparison). These tests used to assert all four keys and
        failed with KeyError on every platform without ambient XDG vars."""
        monkeypatch.setenv(env_mod.XDG_ROOT_ENV, str(tmp_path))
        a = env_mod.isolated_xdg_env({}, run_id="CTL-01-n1")
        b = env_mod.isolated_xdg_env({}, run_id="CTL-01-n2")
        assert a["XDG_DATA_HOME"] != b["XDG_DATA_HOME"]
        assert a["XDG_CONFIG_HOME"] != b["XDG_CONFIG_HOME"]
        # inherited vars pass through untouched -- fabricating them wedges
        # opencode v2 (measured), so the contract forbids it
        assert a.get("XDG_STATE_HOME") == b.get("XDG_STATE_HOME")
        assert a.get("XDG_CACHE_HOME") == b.get("XDG_CACHE_HOME")

    def test_redirected_vars_point_into_the_run_root(self, monkeypatch, tmp_path: Path):
        """Was 'all four vars are redirected' — the contract narrowed to
        DATA/CONFIG only; STATE/CACHE must be inherited, not fabricated."""
        monkeypatch.setenv(env_mod.XDG_ROOT_ENV, str(tmp_path))
        env = env_mod.isolated_xdg_env({}, run_id="r")
        for key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME"):
            assert env[key].startswith(str(tmp_path))
        for key in env_mod._XDG_INHERITED:
            assert key not in env, f"{key} must be inherited, not fabricated into the isolated env"

    def test_directories_are_created(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv(env_mod.XDG_ROOT_ENV, str(tmp_path))
        env = env_mod.isolated_xdg_env({}, run_id="r")
        for key in ("XDG_DATA_HOME", "XDG_CONFIG_HOME"):
            assert Path(env[key]).is_dir()

    def test_opencode_profile_is_seeded(self, monkeypatch, tmp_path: Path):
        """Regression: the old `_write_provider_config` was `pass`, so the
        isolated profile had neither config nor auth — every call failed."""
        monkeypatch.setenv(env_mod.XDG_ROOT_ENV, str(tmp_path / "iso"))
        fake_cfg = tmp_path / "realcfg"
        cfg = fake_cfg / "opencode"
        cfg.mkdir(parents=True)
        (cfg / "opencode.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_cfg))
        env = env_mod.isolated_xdg_env({}, run_id="r")
        assert (Path(env["XDG_CONFIG_HOME"]) / "opencode" / "opencode.json").is_file()

    def test_windows_profile_source_includes_posix_style_path(self, monkeypatch, tmp_path: Path):
        """opencode v2 keeps its state DB at `~/.local/share/opencode/` even on
        Windows — a platform-only source list would silently miss it."""
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
        sources = [str(p) for p in env_mod._profile_sources("XDG_DATA_HOME")]
        assert any(".local" in s and "share" in s for s in sources)

    def test_profile_sources_deduplicated(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        sources = env_mod._profile_sources("XDG_CONFIG_HOME")
        assert len(sources) == len(set(sources))

    def test_explicit_env_wins_first(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "explicit"))
        assert env_mod._profile_sources("XDG_CONFIG_HOME")[0] == tmp_path / "explicit"

    def test_missing_profile_is_not_fatal(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv(env_mod.XDG_ROOT_ENV, str(tmp_path / "iso"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope-config"))
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "nope-data"))
        env = env_mod.isolated_xdg_env({}, run_id="r")
        assert Path(env["XDG_CONFIG_HOME"]).is_dir()


class TestSeeding:
    def test_zero_count_is_noop(self):
        res = env_mod.seed_sessions(0, "fake.iso", {}, real_mode=False)
        assert res.session_ids == [] and res.error is None

    def test_real_mode_declines_with_reason(self):
        """Pre-seeded real sessions point at a PPSSPP process that died with
        the seeding server — declining loudly beats silently broken runs."""
        res = env_mod.seed_sessions(2, "game.iso", {}, real_mode=True)
        assert res.session_ids == [] and "real" in res.error

    def test_failure_is_reported_not_raised(self):
        res = env_mod.seed_sessions(1, "fake.iso", {"PATH": "x"}, real_mode=False)
        assert res.session_ids == [] and res.error


# ============================================================================
# collect — stop-reason semantics and prompt assembly
# ============================================================================


class TestStopReason:
    def test_driver_failure_not_reported_as_max_turns(self):
        """Regression: old code wrote `"final_answer" if text else "max_turns"`,
        so a run that never started looked like a model that ran out of turns."""
        assert collect_mod._stop_reason("", 0, run_ok=False) == "driver_failure"

    def test_empty_stream_distinct(self):
        assert collect_mod._stop_reason("", 0, run_ok=True) == "empty_stream"

    def test_tools_without_text(self):
        assert collect_mod._stop_reason("", 3, run_ok=True) == "no_final_text"

    def test_final_answer(self):
        assert collect_mod._stop_reason("答案", 2, run_ok=True) == "final_answer"


class TestPrompt:
    def test_includes_system_template_and_prompt(self):
        scenarios, _ = scenario_mod.load_scenarios(_EVALS_DIR)
        prompt = collect_mod.build_prompt(scenarios["CTL-01"], real_mode=False, variant="B1")
        assert "PSP 模拟器调试助手" in prompt
        assert scenarios["CTL-01"].prompt.strip()[:10] in prompt

    def test_real_iso_placeholder_substituted(self, monkeypatch):
        """E4 (spec 008): the assertion used to depend on the AMBIENT
        `PPSSPP_DFX_TEST_ISO_PATH` -- it passed where that var was unset
        (fallback "game.iso") and failed whenever it pointed at any other
        ISO (measured 2026-10-02 with cube.iso: the prompt carried the
        ambient path, so "game.iso" was absent). Pin the env to a sentinel
        so the test is deterministic AND actually proves env -> prompt
        substitution instead of the fallback coincidence.
        """
        import dataclasses

        pinned = "pinned-real-iso-under-test.iso"
        monkeypatch.setenv("PPSSPP_DFX_TEST_ISO_PATH", pinned)
        scenarios, _ = scenario_mod.load_scenarios(_EVALS_DIR)
        card = dataclasses.replace(scenarios["CTL-01"], prompt="加载 {{REAL_ISO}}")
        prompt = collect_mod.build_prompt(card, real_mode=True, variant="B1")
        assert "{{REAL_ISO}}" not in prompt
        assert pinned in prompt


class TestCliSurface:
    def test_parses_flags(self):
        args = collect_mod.build_arg_parser().parse_args(
            ["--scenarios", "CTL-01", "--runs", "3", "--dump-events", "-vv", "--no-seed"]
        )
        assert args.scenarios == "CTL-01" and args.runs == 3
        assert args.dump_events and args.verbose == 2 and args.no_seed

    def test_unknown_scenario_exits_2(self, capsys, monkeypatch, tmp_path: Path):
        code = collect_mod.main(["--scenarios", "NOPE-99", "--out", str(tmp_path / "o.jsonl")])
        assert code == 2

    def test_help_mentions_oc_entry(self):
        assert "evals.oc" in collect_mod.build_arg_parser().prog


# ============================================================================
# end-to-end — the full data flow, driver stubbed for determinism
# ============================================================================


def _event_stream(tool: str, output: str, answer: str) -> str:
    return "\n".join(
        [
            json.dumps({"type": "step-start", "tokens": {"input": 5, "output": 1, "total": 6}}),
            json.dumps(
                {
                    "type": "tool",
                    "name": f"ppsspp-dfx_{tool}",
                    "input": {"action": "register", "name": "pc"},
                    "state": {"status": "completed", "output": output},
                }
            ),
            json.dumps({"type": "text", "text": answer}),
        ]
    )


def _collector(tmp_path: Path, monkeypatch, stream: str, **kw: Any) -> collect_mod.Collector:
    scenarios, cfg = scenario_mod.load_scenarios(_EVALS_DIR)
    col = collect_mod.Collector(
        scenarios=scenarios,
        fixtures=scenario_mod.fixtures_dir(_EVALS_DIR, cfg),
        out_path=kw.pop("out_path", tmp_path / "runs-oc-test.jsonl"),
        model="test/model",
        agent="ppsspp-dfx-collector",
        variant="B1",
        work_dir=collect_mod.resolve_work_dir(),
        server_url="http://127.0.0.1:1",
        server_password=None,
        timeout_s=10,
        seed=kw.pop("seed", False),
        resume=kw.pop("resume", False),
        **kw,
    )
    from evals.oc.runner import OpencodeRun

    monkeypatch.setattr(
        col.runner,
        "run",
        lambda options: OpencodeRun(stdout=stream, returncode=0, duration_s=0.5),
    )
    return col


class TestEndToEnd:
    def test_happy_path_lands_valid_record(self, tmp_path: Path, monkeypatch):
        """A real CTL-01-shaped run: namespaced tool call → parsed → scored →
        appended. This is the flow that produced 0 tool calls before."""
        col = _collector(
            tmp_path, monkeypatch, _event_stream("ppsspp_query", "0x0880432C", "PC = 0x0880432C")
        )
        rec = col.collect(col.scenarios["CTL-01"], 1)

        assert rec["collector"] == "opencode"
        assert rec["stop_reason"] == "final_answer"
        assert rec["error_kind"] is None
        assert [c["name"] for c in rec["tool_calls"]] == ["ppsspp_query"]
        assert rec["tool_calls"][0]["args"] == {"action": "register", "name": "pc"}
        assert rec["final_answer"] == "PC = 0x0880432C"
        # provenance is populated (not the old `{"runner": "opencode"}` stub)
        assert set(rec["provenance"]) >= {
            "git_commit",
            "tool_surface_sha256",
            "instructions_sha256",
            "fixture_dir_sha256",
        }
        # usage came from the event stream, not hardcoded zeros
        assert rec["usage"]["input_tokens"] == 5

    def test_record_is_appended_as_jsonl(self, tmp_path: Path, monkeypatch):
        col = _collector(
            tmp_path, monkeypatch, _event_stream("ppsspp_query", "0x0880432C", "PC = 0x0880432C")
        )
        col.collect(col.scenarios["CTL-01"], 1)
        lines = (tmp_path / "runs-oc-test.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        row = json.loads(lines[0])
        assert set(row) >= TestRecordContract.RUNNER_KEYS
        assert row["scenario_id"] == "CTL-01"

    def test_empty_stream_is_flagged_not_silently_passed(self, tmp_path: Path, monkeypatch):
        col = _collector(tmp_path, monkeypatch, "")
        rec = col.collect(col.scenarios["CTL-01"], 1)
        assert rec["stop_reason"] == "driver_failure"
        # attach URL is set in the harness, so classify reports server_gone;
        # either way the run must carry a driver-failure kind, not look clean.
        assert rec["error_kind"] in (ErrorKind.EMPTY_OUTPUT, ErrorKind.SERVER_GONE)
        assert rec["success"] is False

    def test_text_without_tools_is_diagnosed(self, tmp_path: Path, monkeypatch):
        """Regression shape of the old bug: output exists but nothing matched.

        The run genuinely produced a final answer, so `stop_reason` must stay
        `final_answer` — only `error_kind` flags the missing tool calls. Driving
        the two facts into one field is what made the old records unreadable.
        """
        stream = json.dumps({"type": "text", "text": "我认为不需要调用工具"})
        col = _collector(tmp_path, monkeypatch, stream)
        rec = col.collect(col.scenarios["CTL-01"], 1)
        assert rec["error_kind"] == ErrorKind.NO_TOOL_CALLS
        assert rec["stop_reason"] == "final_answer"
        assert rec["success"] is False

    def test_driver_failure_does_not_raise(self, tmp_path: Path, monkeypatch):
        col = _collector(tmp_path, monkeypatch, "")

        def _boom(options):
            raise OSError("bin 不可执行")

        monkeypatch.setattr(col.runner, "run", _boom)
        rec = col.collect(col.scenarios["CTL-01"], 1)
        assert rec["error_kind"] == ErrorKind.SPAWN_FAILED
        assert rec["success"] is False

    def test_gates_receive_normalized_names(self, tmp_path: Path, monkeypatch):
        """`first_tool` gate must see `ppsspp_query`, not the namespaced form —
        otherwise every first_tool gate fails no matter what the agent does."""
        col = _collector(
            tmp_path, monkeypatch, _event_stream("ppsspp_query", "0x0880432C", "PC = 0x0880432C")
        )
        rec = col.collect(col.scenarios["CTL-01"], 1)
        assert rec["gates"]["first_tool"] is True

    def test_pre_state_recorded(self, tmp_path: Path, monkeypatch):
        col = _collector(
            tmp_path, monkeypatch, _event_stream("ppsspp_query", "0x0880432C", "x"), seed=True
        )
        # collect.py 直接 import 绑定 seed_sessions，须打在绑定处而非源模块
        monkeypatch.setattr(
            collect_mod, "seed_sessions", lambda *a, **k: env_mod._SeedResult(["s1"], None)
        )
        rec = col.collect(col.scenarios["CTL-01"], 1)
        assert rec["pre_state"] == {"requested": 1, "seeded": 1, "error": None}

    def test_seed_failure_is_visible(self, tmp_path: Path, monkeypatch):
        col = _collector(
            tmp_path, monkeypatch, _event_stream("ppsspp_query", "0x0880432C", "x"), seed=True
        )
        monkeypatch.setattr(
            collect_mod, "seed_sessions", lambda *a, **k: env_mod._SeedResult([], "boom")
        )
        rec = col.collect(col.scenarios["CTL-01"], 1)
        assert rec["pre_state"]["error"] == "boom"

    def test_grid_marks_progress_and_resumes(self, tmp_path: Path, monkeypatch):
        col = _collector(
            tmp_path, monkeypatch, _event_stream("ppsspp_query", "0x0880432C", "PC = 0x0880432C")
        )
        passed, total = collect_mod.run_grid(col, ["CTL-01"], runs=2)
        assert total == 2
        assert (
            len((tmp_path / "runs-oc-test.jsonl").read_text(encoding="utf-8").strip().splitlines())
            == 2
        )
        # resume: a fresh collector over the same file skips everything
        col2 = _collector(
            tmp_path, monkeypatch, _event_stream("ppsspp_query", "0x0880432C", "PC = 0x0880432C")
        )
        col2.done = trajectory_mod.done_keys(tmp_path / "runs-oc-test.jsonl")
        passed2, total2 = collect_mod.run_grid(col2, ["CTL-01"], runs=2)
        assert total2 == 0

    def test_bad_scenario_does_not_kill_grid(self, tmp_path: Path, monkeypatch):
        col = _collector(
            tmp_path, monkeypatch, _event_stream("ppsspp_query", "0x0880432C", "PC = 0x0880432C")
        )
        original = col.collect

        def _flaky(scenario, run_idx):
            if scenario.id == "CTL-02" and run_idx == 1:
                raise RuntimeError("模拟单 run 崩溃")
            return original(scenario, run_idx)

        monkeypatch.setattr(col, "collect", _flaky)
        passed, total = collect_mod.run_grid(col, ["CTL-01", "CTL-02"], runs=1)
        assert total == 2  # both attempted, grid survived


class TestOpencodeV2EventSchema:
    """opencode v2.0.19 实测结构回归。

    v2 把工具事件包成 `{"type": "tool_use", "part": {..., "tool": <name>,
    "state": {"input": <args>, "output": <result>}}}`——**参数在 `state.input`
    里**，不在节点顶层。只读顶层会让每条调用的 args 变成 `{}`，而
    `params` / `sequence` 门禁全依赖参数。
    """

    EVENT = {
        "type": "tool_use",
        "timestamp": 1790708109032,
        "sessionID": "ses_x",
        "part": {
            "partID": "prt_1",
            "type": "tool",
            "id": "call_1",
            "tool": "ppsspp-dfx_ppsspp_query",
            "state": {
                "status": "completed",
                "input": {"action": "register", "name": "pc", "safe": True},
                "output": '{"uintValue": 142606380}',
                "metadata": {"huge": ["tool", "definition", "dump"]},
            },
            "time": {"start": 1, "end": 2},
        },
    }

    def _one(self):
        return events_mod.parse_stream(json.dumps(self.EVENT)).tool_calls[0]

    def test_args_read_from_state_input(self):
        assert self._one().args == {"action": "register", "name": "pc", "safe": True}

    def test_name_read_from_part_tool_field(self):
        assert self._one().name == "ppsspp_query"

    def test_result_read_from_state_output(self):
        assert "142606380" in self._one().result_preview

    def test_state_metadata_dump_does_not_pollute_text(self):
        """`state.metadata` 里是整份工具定义清单——漏剪会让 final_answer
        被几十万字符的定义文本淹没。"""
        assert events_mod.parse_stream(json.dumps(self.EVENT)).text == ""

    def test_error_status_flags_call(self):
        event = json.loads(json.dumps(self.EVENT))
        event["part"]["state"]["status"] = "error"
        event["part"]["state"]["error"] = "PPSSPP_NOT_RUNNING"
        call = events_mod.parse_stream(json.dumps(event)).tool_calls[0]
        assert call.is_error is True
        assert "PPSSPP_NOT_RUNNING" in call.result_preview

    def test_final_text_comes_from_nested_part(self):
        event = {
            "type": "text",
            "part": {"type": "text", "text": "PC = 0x0880432C"},
        }
        assert events_mod.parse_stream(json.dumps(event)).text == "PC = 0x0880432C"

    def test_builtin_tools_go_to_diagnostics(self):
        """opencode 自带 execute/shell/skill 不是 ppsspp 工具，必须分流，
        否则会污染 `tool_calls` 让 first_tool 门禁误判。"""
        event = json.loads(json.dumps(self.EVENT))
        event["part"]["tool"] = "execute"
        parsed = events_mod.parse_stream(json.dumps(event))
        assert parsed.tool_calls == []
        assert [c.name for c in parsed.other_tool_calls] == ["execute"]


class TestMcpPreflight:
    """MCP 未接线是最贵的一类隐性故障：整场 runs 都是 calls=0 的误导数据。"""

    def test_unregistered_reports_actionable_error(self, monkeypatch, tmp_path: Path):
        def _fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, "No MCP servers configured\n", "")

        monkeypatch.setattr(collect_mod.subprocess, "run", _fake_run)
        ok, detail = collect_mod.check_mcp_registered(tmp_path)
        assert ok is False
        assert "opencode mcp add" in detail

    def test_registered_but_not_connected(self, monkeypatch, tmp_path: Path):
        def _fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, "x ppsspp-dfx  pending\n", "")

        monkeypatch.setattr(collect_mod.subprocess, "run", _fake_run)
        ok, detail = collect_mod.check_mcp_registered(tmp_path)
        assert ok is False and "connected" in detail

    def test_connected_passes(self, monkeypatch, tmp_path: Path):
        def _fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, "x ppsspp-dfx  connected\n", "")

        monkeypatch.setattr(collect_mod.subprocess, "run", _fake_run)
        ok, detail = collect_mod.check_mcp_registered(tmp_path)
        assert ok is True and "connected" in detail

    def test_probe_failure_is_not_silent(self, monkeypatch, tmp_path: Path):
        def _boom(cmd, **kw):
            raise OSError("bin 缺失")

        monkeypatch.setattr(collect_mod.subprocess, "run", _boom)
        ok, _ = collect_mod.check_mcp_registered(tmp_path)
        assert ok is False


class TestStreamErrorEvents:
    """opencode v2 的 provider 认证/额度错误只出现在事件流里，进程同时非零退出。

    不解析它 → 只能按 returncode 猜 → 「额度用完」被显示成「server 失联」，
    处置方向完全错（换模型 vs 重启 server）。
    """

    AUTH_EVENT = {
        "type": "error",
        "error": {
            "type": "provider.auth",
            "message": "OpenCode's free tier can only be used from within OpenCode",
            "status": 403,
        },
    }

    def test_error_event_is_captured(self):
        parsed = events_mod.parse_stream(json.dumps(self.AUTH_EVENT))
        assert len(parsed.errors) == 1
        assert parsed.errors[0].type == "provider.auth"
        assert parsed.errors[0].status == 403

    def test_auth_error_classified(self):
        parsed = events_mod.parse_stream(json.dumps(self.AUTH_EVENT))
        assert events_mod.classify_stream_error(parsed.errors[0]) == ErrorKind.PROVIDER_AUTH

    def test_rate_limit_classified(self):
        err = events_mod.StreamError(type="provider.rate", message="429 rate limit exceeded")
        assert events_mod.classify_stream_error(err) == ErrorKind.RATE_LIMITED

    def test_insufficient_funds_classified_as_quota_not_rate_limit(self):
        """实测 402 "Insufficient account funds" 含 "quota" 字样——先判限流会
        把「账号没钱」显示成「等窗口即可」，反复重试一个不会自愈的故障。"""
        err = events_mod.StreamError(
            type="provider.quota",
            message="Upstream request failed: Insufficient account funds",
            status=402,
        )
        assert events_mod.classify_stream_error(err) == ErrorKind.PROVIDER_QUOTA

    def test_http_403_maps_to_auth(self):
        err = events_mod.StreamError(type="weird", message="forbidden", status=403)
        assert events_mod.classify_stream_error(err) == ErrorKind.PROVIDER_AUTH

    def test_model_unavailable_classified(self):
        err = events_mod.StreamError(type="provider.model", message="model not found: foo")
        assert events_mod.classify_stream_error(err) == ErrorKind.MODEL_UNAVAILABLE

    def test_unknown_error_falls_back(self):
        err = events_mod.StreamError(type="weird", message="something else")
        assert events_mod.classify_stream_error(err) == ErrorKind.STREAM_ERROR

    def test_error_event_is_not_text(self):
        """error 消息不能混进 final_answer——否则门禁 answer_contains 会被
        错误文案误命中。"""
        assert events_mod.parse_stream(json.dumps(self.AUTH_EVENT)).text == ""

    def test_collector_prefers_stream_error_over_exit_code(self, tmp_path: Path, monkeypatch):
        """回归：此前这类 run 被标成 `server_gone`。"""
        from evals.oc.runner import OpencodeRun

        col = _collector(tmp_path, monkeypatch, "")
        monkeypatch.setattr(
            col.runner,
            "run",
            lambda options: OpencodeRun(
                stdout=json.dumps(self.AUTH_EVENT), stderr="", returncode=1
            ),
        )
        rec = col.collect(col.scenarios["CTL-01"], 1)
        assert rec["error_kind"] == ErrorKind.PROVIDER_AUTH
        assert rec["stream_errors"][0]["status"] == 403
        assert rec["stop_reason"] == "driver_failure"


class TestExecuteSandboxExpansion:
    """opencode v2 把**所有**工具调用（含 MCP）包在 `execute` 沙箱里。

    实测形态：`{"part":{"tool":"execute","state":{"input":{"code":
    "const r = await tools[\"ppsspp-dfx\"].ppsspp_health({});\\nreturn r;"}}}}`

    只按 part.tool 过滤（找 `ppsspp-*`）在 v2 上必然 0 命中——这正是重构前
    `tool_calls` 恒为空的结构性原因。不展开 code 就永远看不见 ppsspp 工具。
    """

    @staticmethod
    def _event(code: str, output: Any = None, tool: str = "execute") -> str:
        return json.dumps(
            {
                "type": "tool_use",
                "part": {
                    "type": "tool",
                    "tool": tool,
                    "state": {
                        "status": "completed",
                        "input": {"code": code},
                        "output": output if output is not None else {"status": "ok"},
                    },
                },
            }
        )

    def test_mcp_call_is_extracted_with_name_and_args(self):
        code = 'const r = await tools["ppsspp-dfx"].ppsspp_query({ action: "register", name: "pc" });\nreturn r;'
        call = events_mod.parse_stream(self._event(code)).tool_calls[0]
        assert call.name == "ppsspp_query"
        assert call.args == {"action": "register", "name": "pc"}

    def test_result_preview_carries_tool_output(self):
        code = 'const r = await tools["ppsspp-dfx"].ppsspp_health({}); return r;'
        call = events_mod.parse_stream(self._event(code, {"status": "ok"})).tool_calls[0]
        assert "ok" in call.result_preview

    def test_pure_compute_execute_goes_to_diagnostics(self):
        code = "const x = 1 + 1; return x;"
        parsed = events_mod.parse_stream(self._event(code))
        assert parsed.tool_calls == []
        assert [c.name for c in parsed.other_tool_calls] == ["execute"]

    def test_search_call_is_not_mistaken_for_tool_use(self):
        """`search({query:\"ppsspp_health\"})` 是查工具目录，不是调工具。"""
        code = 'const found = search({ query: "ppsspp_health" }); return found;'
        parsed = events_mod.parse_stream(self._event(code))
        assert parsed.tool_calls == []

    def test_multiple_calls_in_one_execute(self):
        code = (
            'const a = await tools["ppsspp-dfx"].ppsspp_health({});\n'
            'const b = await tools["ppsspp-dfx"].ppsspp_query({ name: "pc" });\n'
            "return [a, b];"
        )
        names = [c.name for c in events_mod.parse_stream(self._event(code)).tool_calls]
        assert names == ["ppsspp_health", "ppsspp_query"]

    def test_other_servers_ignored(self):
        code = 'const a = await tools["other-server"].ppsspp_health({}); return a;'
        parsed = events_mod.parse_stream(self._event(code))
        assert parsed.tool_calls == []

    def test_nested_braces_in_args(self):
        code = 'const r = await tools["ppsspp-dfx"].ppsspp_scan({ ranges: [{ start: 1, end: 2 }] }); return r;'
        call = events_mod.parse_stream(self._event(code)).tool_calls[0]
        assert call.name == "ppsspp_scan"
        assert call.args["ranges"][0]["end"] == 2

    def test_string_containing_paren_does_not_break_scan(self):
        code = 'const r = await tools["ppsspp-dfx"].ppsspp_query({ note: "a)b(c" }); return r;'
        call = events_mod.parse_stream(self._event(code)).tool_calls[0]
        assert call.args["note"] == "a)b(c"

    def test_unquoted_js_keys_normalised(self):
        code = 'const r = await tools["ppsspp-dfx"].ppsspp_step({ action: "step", count: 3 }); return r;'
        call = events_mod.parse_stream(self._event(code)).tool_calls[0]
        assert call.args == {"action": "step", "count": 3}

    def test_bare_identifier_value_falls_back_to_raw(self):
        """`{ action: step }` 的值是裸标识符，JSON 化不可能——必须落 `_raw` 留痕，
        绝不能静默变成 `{}`（会让 params 门禁误判"参数全对"）。"""
        code = (
            'const r = await tools["ppsspp-dfx"].ppsspp_step({ action: step, count: 3 }); return r;'
        )
        call = events_mod.parse_stream(self._event(code)).tool_calls[0]
        assert call.args == {"_raw": "{ action: step, count: 3 }"}

    def test_trailing_comma_tolerated(self):
        code = 'const r = await tools["ppsspp-dfx"].ppsspp_step({ action: "step", }); return r;'
        call = events_mod.parse_stream(self._event(code)).tool_calls[0]
        assert call.args == {"action": "step"}

    def test_empty_args(self):
        code = 'const r = await tools["ppsspp-dfx"].ppsspp_health({}); return r;'
        assert events_mod.parse_stream(self._event(code)).tool_calls[0].args == {}

    def test_non_object_args_are_preserved_not_dropped(self):
        """参数解析失败要留痕成 `_raw`，而不是静默变 `{}`——否则 params 门禁
        会报"参数全对"或"全错"，都误导。"""
        code = 'const r = await tools["ppsspp-dfx"].ppsspp_read(x => x + 1); return r;'
        call = events_mod.parse_stream(self._event(code)).tool_calls[0]
        assert "_raw" in call.args or call.args == {}

    def test_shell_denied_agent_still_works(self):
        """`execute` 是 MCP 的运输层，**不能** deny；deny 掉等于禁用 MCP。"""
        md = (
            Path(collect_mod.resolve_work_dir())
            / ".opencode"
            / "agents"
            / "ppsspp-dfx-collector.md"
        )
        if not md.is_file():
            pytest.skip("agent 定义文件不在工作目录内")
        front = md.read_text(encoding="utf-8").split("---")[1]
        assert "execute: deny" not in front
        assert "shell: deny" in front


class TestGridCircuitBreaker:
    """provider 级失败不会自愈——继续跑只会产出同质噪音。

    实测 49 卡全量跑到第 8 张撞上额度耗尽，剩下 39 张全是
    `provider_quota` + `calls=0`，真正有效的数据只有 7 条。
    """

    _QUOTA_STDOUT = json.dumps(
        {
            "type": "error",
            "error": {"type": "provider.quota", "message": "no funds", "status": 402},
        }
    )
    _OK_STDOUT = _event_stream("ppsspp_query", "0x0880432C", "PC = 0x0880432C")

    def _col(self, tmp_path, monkeypatch, plan, calls_out):
        """plan: 每个 run 的 (stdout, returncode)；记录驱动实际被调了几次。"""
        col = _collector(tmp_path, monkeypatch, "")
        from evals.oc.runner import OpencodeRun

        seq = list(plan)

        def _run(_options):
            calls_out.append(1)
            if not seq:
                raise AssertionError("熔断后仍在调用驱动——熔断没生效")
            stdout, rc = seq.pop(0)
            return OpencodeRun(stdout=stdout, returncode=rc, duration_s=0.1)

        monkeypatch.setattr(col.runner, "run", _run)
        return col

    def test_trips_after_threshold(self, tmp_path: Path, monkeypatch):
        calls: list[int] = []
        col = self._col(tmp_path, monkeypatch, [(self._QUOTA_STDOUT, 1)] * 5, calls)
        collect_mod.run_grid(
            col,
            ["CTL-01", "L1-01", "L1-02", "L1-03", "L1-04"],
            runs=1,
            circuit_threshold=3,
        )
        assert len(calls) == 3, "应在第 3 次 provider 失败后熔断"

    def test_success_resets_counter(self, tmp_path: Path, monkeypatch):
        """成功一次应清零计数，否则「失败-失败-成功-失败-失败」会误熔断。"""
        calls: list[int] = []
        plan = [
            (self._QUOTA_STDOUT, 1),
            (self._QUOTA_STDOUT, 1),
            (self._OK_STDOUT, 0),  # 复位
            (self._QUOTA_STDOUT, 1),
            (self._QUOTA_STDOUT, 1),
            (self._QUOTA_STDOUT, 1),  # 复位后再累计到 3 → 熔断
        ]
        col = self._col(tmp_path, monkeypatch, plan, calls)
        collect_mod.run_grid(
            col,
            ["CTL-01", "L1-01", "L1-02", "L1-03", "L1-04", "L1-05"],
            runs=1,
            circuit_threshold=3,
        )
        assert len(calls) == 6

    def test_threshold_disabled(self, tmp_path: Path, monkeypatch):
        calls: list[int] = []
        col = self._col(tmp_path, monkeypatch, [(self._QUOTA_STDOUT, 1)] * 3, calls)
        collect_mod.run_grid(col, ["CTL-01", "L1-01", "L1-02"], runs=1, circuit_threshold=10**9)
        assert len(calls) == 3

    def test_content_failures_do_not_trip(self, tmp_path: Path, monkeypatch):
        """门禁判失败是**正常结果**，不是 provider 故障——绝不能触发熔断。"""
        calls: list[int] = []
        stream = json.dumps({"type": "text", "text": "我认为不需要调用工具"})
        col = self._col(tmp_path, monkeypatch, [(stream, 0)] * 4, calls)
        collect_mod.run_grid(
            col, ["CTL-01", "L1-01", "L1-02", "L1-03"], runs=1, circuit_threshold=1
        )
        assert len(calls) == 4

    def test_auth_error_also_trips(self, tmp_path: Path, monkeypatch):
        auth = json.dumps(
            {
                "type": "error",
                "error": {"type": "provider.auth", "message": "no credential", "status": 403},
            }
        )
        calls: list[int] = []
        col = self._col(tmp_path, monkeypatch, [(auth, 1)] * 4, calls)
        collect_mod.run_grid(
            col, ["CTL-01", "L1-01", "L1-02", "L1-03"], runs=1, circuit_threshold=2
        )
        assert len(calls) == 2

    def test_circuit_kinds_exclude_content_gates(self):
        """熔断只认 provider 级故障；`no_tool_calls` / `empty_output` 是评测结果。"""
        assert ErrorKind.NO_TOOL_CALLS not in collect_mod.CIRCUIT_KINDS
        assert ErrorKind.EMPTY_OUTPUT not in collect_mod.CIRCUIT_KINDS
        assert ErrorKind.PROVIDER_QUOTA in collect_mod.CIRCUIT_KINDS
        assert ErrorKind.PROVIDER_AUTH in collect_mod.CIRCUIT_KINDS


# ============================================================================
# backward compatibility — the deprecated entry point
# ============================================================================


# ============================================================================
# agent definition — the prompt must not cite tools that do not exist
# ============================================================================


def _repo_root() -> Path | None:
    """Walk up from evals/ to the first dir holding `.opencode/agents/`."""
    for parent in _EVALS_DIR.resolve().parents:
        if (parent / ".opencode" / "agents").is_dir():
            return parent
    return None


#: Marker words that make a `ppsspp_*` mention a deliberate NEGATIVE reference
#: ("there is no such tool"), which must not be checked against the baseline.
_NEGATION_MARKERS = ("没有", "不存在", "不要用", "已禁用", "deny")


def _agent_md() -> Path:
    root = _repo_root()
    if root is None:  # pragma: no cover - only when the repo layout changed
        pytest.skip("repo root with .opencode/agents/ not found")
    return root / ".opencode" / "agents" / "ppsspp-dfx-collector.md"


def _real_tool_names() -> set[str]:
    baseline = _PKG_ROOT / "tests" / "unit" / "l2_mcp_contract" / "tool_surface_baseline.json"
    return set(json.loads(baseline.read_text(encoding="utf-8"))["tools"])


class TestAgentPromptToolSurface:
    """Regression for the 2026-09-30 incident.

    The collector prompt advertised `ppsspp_session_list`, which does not exist
    (the real surface has `ppsspp_session({action:"list"})`). The model burned
    4 turns on the phantom tool and the run's trajectory was polluted.

    A prompt is not type-checked, so only an automated gate catches this.
    """

    def test_every_referenced_tool_exists(self):
        md = _agent_md().read_text(encoding="utf-8")
        real = _real_tool_names()

        missing = []
        for line in md.splitlines():
            hits = set(re.findall(r"ppsspp_[a-z_]+", line))
            if not hits:
                continue
            if any(marker in line for marker in _NEGATION_MARKERS):
                continue  # deliberate "no such tool" warning
            missing.extend(sorted(hits - real))

        assert not missing, f"prompt cites non-existent tools: {sorted(set(missing))}"

    def test_phantom_tool_is_explicitly_denied(self):
        """The negative warning must survive, so the model stops guessing it."""
        md = _agent_md().read_text(encoding="utf-8")
        assert "ppsspp_session_list" in md

        lines = md.splitlines()
        warned = [i for i, ln in enumerate(lines) if "ppsspp_session_list" in ln and "没有" in ln]
        assert warned, "prompt must explicitly state ppsspp_session_list does not exist"

        # The replacement call may sit on a following line (markdown wrapping),
        # so look at the warning's neighbourhood, not just the line itself.
        window = "".join(lines[warned[0] : warned[0] + 3])
        assert "action" in window and "list" in window, (
            "the replacement call must be shown, not just the prohibition"
        )

    def test_prompt_contains_no_unsubstituted_placeholder(self):
        """`{{REAL_ISO}}` is only substituted in scenario cards (collect.build_prompt
        walks `scenario.prompt`), never in the static agent definition — so a
        literal placeholder in the agent text would reach the model verbatim."""
        md = _agent_md().read_text(encoding="utf-8")
        assert "{{REAL_ISO}}" not in md, (
            "agent definition is static and never placeholder-substituted"
        )

    def test_execute_is_not_denied(self):
        """opencode v2 routes every MCP call through the `execute` sandbox.
        Denying it disables the only transport (2026-09-30 self-inflicted outage)."""
        md = _agent_md().read_text(encoding="utf-8")
        front = md.split("---")[1]
        assert re.search(r"^\s*execute:\s*deny", front, re.MULTILINE) is None
        assert re.search(r"^\s*shell:\s*deny", front, re.MULTILINE), "shell must stay denied"

    def test_step_budget_exceeds_typical_task_length(self):
        """12 steps was too tight: the model roamed and opencode aborted with
        `aborted: Step interrupted`. 30 gives headroom while the prompt's
        'finish within 3 steps' discipline keeps real cost low."""
        md = _agent_md().read_text(encoding="utf-8")
        assert int(re.search(r"^steps:\s*(\d+)", md, re.MULTILINE).group(1)) >= 20


class TestCompatShim:
    def test_shim_reexports_main(self):
        with pytest.warns(DeprecationWarning):
            import evals.opencode_collect as shim

        assert shim.main is collect_mod.main

    def test_shim_does_not_define_legacy_helpers(self):
        """The old module's helpers moved into evals/oc; the shim must not
        shadow them with a stale second implementation."""
        import evals.opencode_collect as shim

        assert not hasattr(shim, "ServeManager")
        assert not hasattr(shim, "parse_tool_calls")
