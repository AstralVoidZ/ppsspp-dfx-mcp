"""Blind-eval runner for ppsspp-dfx-mcp (fake mode).

Launches ONE fake-mode server subprocess per run (fresh sessions.json
  per run, mirroring tests/mcp_inspector/conftest.py's launch recipe);
- injects ONLY: fixed system template + MCP list_tools output verbatim
  (+ server instructions for B1, omitted for B0) + the scenario prompt;
- drives an OpenAI-compatible agent loop (endpoint + key from a local
  llm_api.json, kept outside the repo) with rate limiting (min interval,
  retry with backoff) because the API is slow;
- records a JSONL trajectory per run and scores it with evals.gates;
- resume-safe: (scenario, model, variant, run_idx) already present in
  the output JSONL are skipped.

Usage (from mcps/ppsspp-dfx-mcp/):
  <venv python> -m evals.runner --scenarios CTL-01 --runs 1
  <venv python> -m evals.runner                 # full grid per config
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_EVALS_DIR = Path(__file__).resolve().parent      # .../ppsspp-dfx-mcp/evals
_PKG_ROOT = _EVALS_DIR.parent                     # .../ppsspp-dfx-mcp
_SRC_ROOT = _PKG_ROOT / "src"
_TESTS_ROOT = _PKG_ROOT / "tests"
_REPO_ROOT = _PKG_ROOT.parents[1]

SYSTEM_TEMPLATE = (
    "你是一个使用 MCP 工具的 PSP 模拟器调试助手。\n"
    "请根据任务需要选择并调用可用的工具；完成任务后，用中文给出最终答案。\n"
)

_ARG_PARSE_CODE = "ARG_PARSE"


# ---------------------------------------------------------------------------
# Config / provenance helpers
# ---------------------------------------------------------------------------

def load_config(path: Path) -> dict[str, Any]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    api_path = Path(os.environ.get(
        "PPSSPP_DFX_EVALS_LLM_API_PATH",
        (_EVALS_DIR / cfg["llm_api_path"]),
    )).resolve()
    if not api_path.is_file():
        raise RuntimeError(
            f"LLM API config not found: {api_path} — set llm_api_path in "
            "evals/config.yaml (or env PPSSPP_DFX_EVALS_LLM_API_PATH) to a "
            "JSON file with {provider: {options: {baseURL, apiKey}, models}}. "
            "This file contains secrets and must stay outside the repo."
        )
    api = json.loads(api_path.read_text(encoding="utf-8"))
    provider = api[cfg["model"]["provider"]]
    opts = provider["options"]
    model_name = cfg["model"]["name"]
    if model_name not in provider.get("models", {}):
        raise ValueError(f"model {model_name!r} not listed under provider in {api_path}")
    cfg["_llm"] = {"base_url": opts["baseURL"], "api_key": opts["apiKey"]}
    return cfg


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _dir_sha256(path: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(path.glob("*")):
        if f.is_file():
            h.update(f.name.encode())
            h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def build_provenance(instructions: str | None) -> dict[str, str]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=_REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception:
        commit = "unknown"
    baseline = _TESTS_ROOT / "unit" / "l2_mcp_contract" / "tool_surface_baseline.json"
    fixtures = _TESTS_ROOT / "cassettes" / "fixtures"
    return {
        "git_commit": commit,
        "tool_surface_sha256": _sha256_file(baseline) if baseline.is_file() else "missing",
        "instructions_sha256": hashlib.sha256(
            (instructions or "").encode("utf-8")
        ).hexdigest(),
        "fixture_dir_sha256": _dir_sha256(fixtures) if fixtures.is_dir() else "missing",
        "runner_version": "v0.1.0",
    }


# ---------------------------------------------------------------------------
# LLM client (OpenAI-compatible, rate-limited)
# ---------------------------------------------------------------------------

class LlmClient:
    def __init__(self, base_url: str, api_key: str, *, min_interval_s: float,
                 max_retries: int, timeout_s: float):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._min_interval = min_interval_s
        self._max_retries = max_retries
        self._timeout = timeout_s
        self._last_call = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    async def _throttle(self) -> None:
        wait = self._min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()

    @staticmethod
    def _post_sync(url: str, payload: dict[str, Any], headers: dict[str, str],
                   timeout_s: float) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    async def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._key}"}
        last_err: Exception | None = None
        for attempt in range(self._max_retries + 1):
            await self._throttle()
            # asyncio-level hard deadline: the socket timeout inside
            # urllib has proven unreliable on this path (observed 20-min
            # hangs), so every attempt is force-cancelled from the event
            # loop regardless of socket state.
            attempt_budget = self._timeout + 15.0
            try:
                data = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._post_sync, f"{self._base}/chat/completions",
                        payload, headers, self._timeout,
                    ),
                    timeout=attempt_budget,
                )
                usage = data.get("usage") or {}
                self.total_input_tokens += int(usage.get("prompt_tokens") or 0)
                self.total_output_tokens += int(usage.get("completion_tokens") or 0)
                return data
            except urllib.error.HTTPError as exc:
                last_err = exc
                retryable = exc.code in (429, 500, 502, 503, 504)
            except Exception as exc:  # noqa: BLE001 — retry transport-level hiccups
                last_err = exc
                retryable = True
            if attempt < self._max_retries:
                if not retryable:
                    break
                wait_s = self._min_interval * (2 ** attempt)
                print(f"    llm retry {attempt + 1}/{self._max_retries} "
                      f"after {type(last_err).__name__}: {str(last_err)[:120]} "
                      f"(wait {wait_s:.0f}s)", flush=True)
                await asyncio.sleep(wait_s)
        raise RuntimeError(f"LLM call failed: {last_err}")


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

def _tool_defs(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    defs = []
    for t in mcp_tools:
        schema = (getattr(t, "input_schema", None)
                  or getattr(t, "inputSchema", None)
                  or {"type": "object", "properties": {}})
        defs.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": schema,
            },
        })
    return defs


def _result_text(result: Any) -> str:
    """Flatten a tool result into the text the agent (and the gates) see.

    Three channels matter, and all three must be represented:

    - **TextContent** — the classic channel.
    - **structuredContent** — after the `tool-schema-contract` change most
      tools answer here, and the image tools answer *only* here for their
      metadata (file_path / size_bytes / empty). Omitting it made
      image-tool scenarios unpassable: the model saw "(empty result)" and
      the `file_saved` gate regexed an empty preview.
    - **ImageContent** — pixels, which no text model can read. It is
      summarised rather than dropped so the model still learns that an
      image arrived (and its size), instead of concluding the call was
      empty.
    """
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
        elif getattr(block, "data", None) is not None:
            mime = getattr(block, "mime_type", "image")
            raw = str(getattr(block, "data", ""))
            parts.append(f"[ImageContent {mime}, {len(raw)} base64 chars]")
    sc = (getattr(result, "structured_content", None)
          or getattr(result, "structuredContent", None))
    if sc:
        parts.append(json.dumps(sc, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# B2 variant: skill pseudo-tool (progressive-disclosure simulation)
# ---------------------------------------------------------------------------

SKILL_DIR = Path(os.environ.get(
    "PPSSPP_DFX_SKILL_DIR", str(_REPO_ROOT / ".zcode" / "skills" / "ppsspp-dfx"),
)).resolve()
SKILL_TOOL_NAME = "skill_read"
_SKILL_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": SKILL_TOOL_NAME,
        "description": (
            "Read a documentation file from the ppsspp-dfx skill bundle "
            "(usage guide, tool surface reference, error-code recovery "
            "table). Use this when a task touches the PPSSPP debugging "
            "workflow and the tool descriptions alone leave questions. "
            "Path is relative to the skill root, e.g. 'SKILL.md' or "
            "'references/error-codes.md'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the skill root.",
                },
            },
            "required": ["path"],
        },
    },
}


def read_skill_file(rel_path: str, skill_dir: Path) -> str:
    """Resolve and read a file inside the skill dir (traversal-guarded)."""
    root = skill_dir.resolve()
    target = (root / rel_path).resolve()
    if not target.is_relative_to(root):
        return f"[skill_read] path escapes skill root: {rel_path}"
    if not target.is_file():
        return f"[skill_read] file not found: {rel_path}"
    return target.read_text(encoding="utf-8")


async def _seed_sessions(session: ClientSession, count: int, iso_path: str) -> list[str]:
    ids: list[str] = []
    for _ in range(max(0, count)):
        result = await session.call_tool(
            "ppsspp_session",
            {"action": "start", "iso_path": iso_path, "wait_ready": True,
             "resilient": True},
        )
        if getattr(result, "is_error", getattr(result, "isError", False)):
            raise RuntimeError(f"pre_state session start failed: {_result_text(result)[:300]}")
        # SDK versions differ on casing: structured_content vs structuredContent.
        sc = (getattr(result, "structured_content", None)
              or getattr(result, "structuredContent", None)) or {}
        sid = sc.get("session_id")
        if not sid:
            # Fallback: SessionResponse JSON is also embedded in the text content.
            try:
                sid = json.loads(_result_text(result)).get("session_id")
            except (json.JSONDecodeError, AttributeError):
                sid = None
        if not sid:
            raise RuntimeError(
                f"pre_state start returned no session_id: {_result_text(result)[:300]}"
            )
        ids.append(str(sid))
    return ids


async def run_scenario(
    cfg: dict[str, Any],
    scenario: dict[str, Any],
    run_idx: int,
    variant: str,
    llm: LlmClient,
    fixtures_dir: Path,
    out_path: Path,
) -> dict[str, Any]:
    from evals.gates import evaluate, extract_error_code

    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    model_tag = cfg["model"]["name"].split("/")[-1][:24]
    run_id = f"r{ts}-{model_tag}-{variant}-{scenario['id']}-n{run_idx}"
    max_turns = int(scenario.get("max_turns", 12))

    sessions_dir = Path(tempfile.mkdtemp(prefix="ppsspp-dfx-eval-sessions-"))
    env = os.environ.copy()
    real_mode = scenario.get("mode") == "real"
    if real_mode:
        # Real PPSSPP: no fake-mode env at all; the project config's
        # ppsspp_exe + the real ISO are used. ppsspp_exe in project.yaml
        # is relative to the REPO root — the server subprocess runs with
        # cwd elsewhere, so pass the absolute path via the env override
        # (same recipe as tests/integration/conftest.py:310).
        env.pop("PPSSPP_DFX_TEST_MODE", None)
        env.pop("PPSSPP_DFX_FIXTURE_DIR", None)
        env["PPSSPP_DFX_EXE_PATH"] = os.environ.get(
            "PPSSPP_DFX_TEST_EXE_PATH", "PPSSPPWindows64.exe"
        )
    else:
        env["PPSSPP_DFX_TEST_MODE"] = "fake"
        env["PPSSPP_DFX_FIXTURE_DIR"] = str(fixtures_dir)
    env["PPSSPP_DFX_LOG_LEVEL"] = "WARNING"
    env["PPSSPP_DFX_SESSIONS_PATH"] = str(sessions_dir / "sessions.json")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_SRC_ROOT), str(_TESTS_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    seed_iso = os.environ.get("PPSSPP_DFX_TEST_ISO_PATH", "game.iso") \
        if real_mode else "fake_game.iso"

    server_params = StdioServerParameters(
        command=sys.executable, args=["-m", "ppsspp_dfx_mcp"], env=env,
    )

    tool_calls: list[dict[str, Any]] = []
    final_answer = ""
    instructions: str | None = None
    stop_reason = "max_turns"
    started = time.monotonic()
    run_timeout_s = float(cfg.get("limits", {}).get("run_timeout_s", 600))

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            instructions = getattr(init, "instructions", None)
            prov = build_provenance(instructions)

            mcp_tools = (await session.list_tools()).tools
            pre_n = int((scenario.get("pre_state") or {}).get("sessions", 0))
            seeded_ids = await _seed_sessions(session, pre_n, seed_iso)
            if real_mode and seeded_ids:
                # Real window settle: PPSSPP needs a few seconds after
                # CPU-ready before the first frame is capturable (an
                # immediate screenshot returns empty=true — R2 first
                # attempt failed entirely on this race).
                await asyncio.sleep(float(cfg.get("limits", {}).get("real_settle_s", 8)))

            sys_prompt = SYSTEM_TEMPLATE
            if variant in ("B1", "B2") and instructions:
                sys_prompt += "\n" + instructions
            elif variant not in ("B0", "B1", "B2"):
                raise ValueError(f"unsupported variant: {variant}")

            messages: list[dict[str, Any]] = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": scenario["prompt"].replace(
                    "{{REAL_ISO}}", seed_iso)},
            ]
            payload_tools = _tool_defs(mcp_tools)
            if variant == "B2":
                if not SKILL_DIR.is_dir():
                    raise RuntimeError(
                        f"B2 variant requires skill dir (not found): {SKILL_DIR}"
                    )
                payload_tools = payload_tools + [dict(_SKILL_TOOL_DEF)]
            caps = int(cfg.get("limits", {}).get("tool_result_char_cap", 8000))

            for _turn in range(max_turns):
                if time.monotonic() - started > run_timeout_s:
                    stop_reason = "run_timeout"
                    break
                if _turn:
                    print(f"    turn {_turn + 1}/{max_turns}, "
                          f"{len(tool_calls)} call(s) so far", flush=True)
                data = await llm.chat({
                    "model": cfg["model"]["name"],
                    "messages": messages,
                    "tools": payload_tools,
                    "tool_choice": "auto",
                    "temperature": 0,
                    "max_tokens": int(cfg.get("limits", {}).get("max_tokens", 4096)),
                })
                msg = (data.get("choices") or [{}])[0].get("message") or {}
                tcs = msg.get("tool_calls") or []
                if not tcs:
                    final_answer = msg.get("content") or ""
                    stop_reason = "final_answer"
                    break
                messages.append({
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": tcs,
                })
                for tc in tcs:
                    name = (tc.get("function") or {}).get("name", "")
                    raw_args = (tc.get("function") or {}).get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                        tool_calls.append({
                            "seq": len(tool_calls) + 1, "name": name, "args": {},
                            "is_error": True, "error_code": _ARG_PARSE_CODE,
                            "result_preview": f"unparseable arguments: {raw_args!r}"[:200],
                            "latency_ms": 0,
                        })
                        messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                         "content": "ERROR: arguments were not valid JSON."})
                        continue
                    t0 = time.monotonic()
                    if name == SKILL_TOOL_NAME:
                        # B2 pseudo tool — answered locally, never touches
                        # the MCP server; flagged pseudo so gates skip it.
                        text = read_skill_file(str(args.get("path") or ""), SKILL_DIR)
                        tool_calls.append({
                            "seq": len(tool_calls) + 1, "name": name, "args": args,
                            "is_error": False, "error_code": None,
                            "result_preview": text[:200],
                            "latency_ms": int((time.monotonic() - t0) * 1000),
                            "pseudo": True,
                        })
                        messages.append({
                            "role": "tool", "tool_call_id": tc.get("id", ""),
                            "content": text[:caps] if text else "(empty result)",
                        })
                        continue
                    try:
                        result = await session.call_tool(name, args)
                        text = _result_text(result)
                        is_error = bool(getattr(result, "is_error",
                                                getattr(result, "isError", False)))
                    except Exception as exc:  # noqa: BLE001 — record as tool-level error
                        text = f"[RUNNER_EXCEPTION] {exc}"
                        is_error = True
                    latency_ms = int((time.monotonic() - t0) * 1000)
                    code = extract_error_code(text) if is_error else None
                    tool_calls.append({
                        "seq": len(tool_calls) + 1, "name": name, "args": args,
                        "is_error": is_error, "error_code": code,
                        "result_preview": text[:500], "latency_ms": latency_ms,
                    })
                    messages.append({
                        "role": "tool", "tool_call_id": tc.get("id", ""),
                        "content": text[:caps] if text else "(empty result)",
                    })
            else:
                stop_reason = "max_turns"

            # Real mode: stop every live session so PPSSPP does not
            # outlive the eval server subprocess (temp sessions.json
            # dies with the run — orphans would be unrecoverable).
            if real_mode:
                for sid in seeded_ids:
                    try:
                        await session.call_tool("ppsspp_session",
                                                {"action": "stop", "session_id": sid})
                    except Exception:  # noqa: BLE001 — best-effort cleanup
                        pass

    gates_result = evaluate(scenario, {"tool_calls": tool_calls,
                                       "final_answer": final_answer}, fixtures_dir)
    record = {
        "run_id": run_id,
        "scenario_id": scenario["id"],
        "model": cfg["model"]["name"],
        "variant": variant,
        "run_idx": run_idx,
        "provenance": prov,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "wall_ms": int((time.monotonic() - started) * 1000),
        "tool_calls": tool_calls,
        "final_answer": final_answer,
        "usage": {
            "input_tokens": llm.total_input_tokens,
            "output_tokens": llm.total_output_tokens,
            "cost_usd": None,
        },
        "stop_reason": stop_reason,
        "gates": {g["type"]: g["pass"] for g in gates_result["gates"]},
        "gate_details": gates_result["gates"],
        "success": gates_result["success"],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


# ---------------------------------------------------------------------------
# Resume support + CLI
# ---------------------------------------------------------------------------

def _done_keys(out_path: Path) -> set[tuple[str, str, str, int]]:
    keys: set[tuple[str, str, str, int]] = set()
    if not out_path.is_file():
        return keys
    for line in out_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            keys.add((r["scenario_id"], r["model"], r["variant"], int(r["run_idx"])))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return keys


async def _async_main(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    scen_cfg = yaml.safe_load((_EVALS_DIR / "scenarios.yaml").read_text(encoding="utf-8"))
    fixtures_dir = (_EVALS_DIR / scen_cfg["fixtures_dir"]).resolve()
    scenarios = {s["id"]: s for s in scen_cfg["scenarios"]}
    wanted = args.scenarios.split(",") if args.scenarios else list(scenarios)
    unknown = [s for s in wanted if s not in scenarios]
    if unknown:
        print(f"unknown scenario ids: {unknown}", file=sys.stderr)
        return 2

    variant = args.variant or cfg.get("variant", "B1")
    n_runs = args.runs if args.runs is not None else int(cfg.get("runs", 5))
    rate = cfg.get("rate", {})
    llm = LlmClient(
        cfg["_llm"]["base_url"], cfg["_llm"]["api_key"],
        min_interval_s=float(rate.get("min_interval_s", 5.0)),
        max_retries=int(rate.get("max_retries", 3)),
        timeout_s=float(rate.get("timeout_s", 120)),
    )
    out_path = _EVALS_DIR / "runs" / f"runs-{datetime.now():%Y%m%d}.jsonl"
    done = _done_keys(out_path)

    total = passed = 0
    for sid in wanted:
        scenario = scenarios[sid]
        for run_idx in range(1, n_runs + 1):
            key = (sid, cfg["model"]["name"], variant, run_idx)
            if key in done:
                print(f"[skip] {key} already recorded")
                continue
            total += 1
            print(f"[run ] {key} ...", flush=True)
            try:
                rec = await run_scenario(cfg, scenario, run_idx, variant, llm,
                                         fixtures_dir, out_path)
            except BaseException as exc:  # noqa: BLE001 — one bad run must not kill the grid
                # anyio TaskGroups wrap real errors in (possibly nested)
                # ExceptionGroups — unwrap recursively to the root causes.
                def roots(e: BaseException) -> list[BaseException]:
                    subs = getattr(e, "exceptions", None)
                    if subs:
                        out: list[BaseException] = []
                        for s in subs:
                            out.extend(roots(s))
                        return out
                    return [e]
                print(f"[FAIL] {key}:", file=sys.stderr, flush=True)
                for r in roots(exc):
                    print(f"  root: {type(r).__name__}: {r}", file=sys.stderr, flush=True)
                continue
            passed += int(rec["success"])
            status = "PASS" if rec["success"] else "FAIL"
            failed_gates = [g for g, ok in rec["gates"].items() if not ok]
            print(f"[{status}] {sid} n={run_idx} calls={len(rec['tool_calls'])} "
                  f"stop={rec['stop_reason']} failed_gates={failed_gates}", flush=True)
    print(f"\ndone: {passed}/{total} runs passed; JSONL → {out_path}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="ppsspp-dfx blind-eval runner (fake mode)")
    p.add_argument("--config", default=str(_EVALS_DIR / "config.yaml"))
    p.add_argument("--scenarios", default="", help="comma-separated scenario ids (default: all)")
    p.add_argument("--runs", type=int, default=None, help="runs per scenario (default: cfg.runs)")
    p.add_argument("--variant", default=None, choices=["B0", "B1", "B2"],
                   help="context variant (default: cfg.variant); B2 adds the "
                        "skill_read pseudo tool")
    args = p.parse_args()
    sys.exit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
