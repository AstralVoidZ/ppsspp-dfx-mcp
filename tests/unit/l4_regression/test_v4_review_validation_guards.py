"""Review-v4 (2026-10-03) validation-guard regression locks.

One file per finding batch so each guard's provenance stays greppable:
- A-1: scan's four range-validation paths must reject start_addr=0x0
  uniformly (pattern-foreground was the hole).
- A-3: query func_add size must be a non-bool int >= 1 (zero/negative
  sizes create invisible, unremovable functions — the tool's own comment
  describes that failure).
- A-4: replay time_set value must be a uint32; execute version must be a
  non-bool int (True bypassed the zero-value gate).
- A-6: scan value must fit the unsigned width — negative/overflow values
  used to silently return "0 candidates" instead of an argument error.
- A-8: query top_n must be >= 0 (negative silently meant "no limit").
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from _support import state as state_seam  # T053 S-4：集中式测试支撑缝

from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.tools import query as query_mod
from ppsspp_dfx_mcp.tools import replay as replay_mod
from ppsspp_dfx_mcp.tools import scan as scan_mod
from ppsspp_dfx_mcp.tools.query import query
from ppsspp_dfx_mcp.tools.replay import replay
from ppsspp_dfx_mcp.tools.scan import scan

# ---------------------------------------------------------------------------
# Shared scaffolding
# ---------------------------------------------------------------------------


async def _resolve(session_id: str | None) -> str:
    return session_id or "sess-fake"


class _FakeScanClient:
    async def read_bytes(self, address: int, size: int) -> list[int]:
        return [0] * size


class _FakeSessionClient:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def __aenter__(self) -> Any:
        return self._client

    async def __aexit__(self, *exc: Any) -> None:
        return None


def _patch_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        scan_mod, "session_client", lambda sid: _FakeSessionClient(_FakeScanClient())
    )
    monkeypatch.setattr(scan_mod, "resolve_session_id", _resolve)

    async def _alive(session_id: str) -> None:
        return None

    monkeypatch.setattr(scan_mod, "validate_session_alive", _alive)
    scan_mod.reset_value_sessions()


def _patch_tool_client(
    monkeypatch: pytest.MonkeyPatch, module: Any, client: AsyncMock | None = None
) -> AsyncMock:
    mock = client if client is not None else AsyncMock()

    @asynccontextmanager
    async def fake_session_client(session_id: str):
        yield mock

    monkeypatch.setattr(module, "session_client", fake_session_client)

    async def _alive(session_id: str) -> None:
        return None

    if hasattr(module, "validate_session_alive"):
        monkeypatch.setattr(module, "validate_session_alive", _alive)
    return mock


# ---------------------------------------------------------------------------
# A-1: every scan path rejects start_addr=0x0
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("background", [False, True], ids=["foreground", "background"])
@pytest.mark.asyncio
async def test_scan_pattern_rejects_start_addr_zero(background, monkeypatch):
    """A-1: pattern-foreground validated `start < end` but not `start > 0`,
    so start=0x0 was accepted in the foreground and rejected in the
    background — same input, two verdicts."""
    _patch_scan(monkeypatch)
    with pytest.raises(ArgsInvalid, match="invalid range"):
        await scan(
            mode="pattern",
            pattern="9090",
            start_addr="0x0",
            end_addr="0x100",
            background=background,
        )


# ---------------------------------------------------------------------------
# A-6: scan value must fit the unsigned width
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [-1, 70000], ids=["negative", "overflow"])
@pytest.mark.asyncio
async def test_scan_value_initial_rejects_invalid_value(value, monkeypatch):
    """A-6: negative and width-overflow values used to fall into the
    OverflowError-to-empty branch — 'scan succeeded, 0 candidates' instead
    of an argument error, steering the caller to keep narrowing."""
    _patch_scan(monkeypatch)
    with pytest.raises(ArgsInvalid, match="unsigned"):
        await scan(
            mode="value",
            phase="initial",
            value=value,
            width="u16",
            start_addr="0x08804000",
            end_addr="0x08805000",
        )


# ---------------------------------------------------------------------------
# A-3: func_add size guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("size", [0, -4, True], ids=["zero", "negative", "bool"])
@pytest.mark.asyncio
async def test_func_add_rejects_invalid_size(size, monkeypatch):
    """A-3: zero/negative/bool sizes must be rejected before the WS call —
    they reproduce the invisible-unremovable-function bug the tool's own
    comment documents."""
    mock = _patch_tool_client(monkeypatch, query_mod)
    with pytest.raises(ArgsInvalid, match="size"):
        await query(
            action="func_add",
            name="fn",
            address="0x08804000",
            size=size,
            session_id="sess-1",
        )
    mock.func_add.assert_not_awaited()


# ---------------------------------------------------------------------------
# A-8: top_n must be >= 0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_top_n_negative_rejected(monkeypatch):
    """A-8: top_n=-1 fell into the same path as 0 ('no limit') and dumped a
    700+KB function table on the caller."""
    mock = _patch_tool_client(monkeypatch, query_mod)
    mock.func_list.return_value = {"functions": [{"name": "f", "address": 1}]}
    with pytest.raises(ArgsInvalid, match="top_n"):
        await query(action="funcs", top_n=-1, session_id="sess-1")


# ---------------------------------------------------------------------------
# A-4: replay value/version guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [-1, 2**32], ids=["negative", "over_uint32"])
@pytest.mark.asyncio
async def test_replay_time_set_rejects_out_of_uint32(value, monkeypatch):
    """A-4: the field description promises 'uint32' but nothing enforced
    it — out-of-range values reached PPSSPP with undefined behavior, and
    the response echoed the original value to mask the wraparound."""
    mock = _patch_tool_client(monkeypatch, replay_mod)
    with pytest.raises(ArgsInvalid, match="uint32"):
        await replay(action="time_set", value=value, session_id="sess-1")
    mock.replay_time_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_execute_rejects_bool_version(monkeypatch):
    """A-4: `version=True` passed the `version == 0` gate via bool→int."""
    mock = _patch_tool_client(monkeypatch, replay_mod)
    with pytest.raises(ArgsInvalid, match="version"):
        await replay(action="execute", version=True, base64_input="AAEC", session_id="sess-1")
    mock.replay_execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# A-5: probe registry keys are stripped (no zombie probes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_observer_register_strips_name(monkeypatch):
    """A-5: register stored the raw name while clear/observe resolve via
    strip() — a " foo " probe could be registered but never pointably
    observed or cleared."""
    from ppsspp_dfx_mcp.tools.state_observer import state_observer

    # T053 S-4：只清注册表、不动播种闩的差别语义由支撑缝的具名函数承载。
    state_seam.clear_probe_registry()
    try:
        await state_observer(
            action="register",
            name="  foo  ",
            address="0x08804000",
            session_id="sess-1",
        )
        listed = await state_observer(action="list", session_id="sess-1")
        names = [p["name"] for p in listed["probes"]]
        assert names == ["foo"], names
        await state_observer(action="clear", name="foo", session_id="sess-1")
        after = await state_observer(action="list", session_id="sess-1")
        assert after["count"] == 0  # the zombie is pointably clearable
    finally:
        state_seam.clear_probe_registry()


# ---------------------------------------------------------------------------
# A-2 / A-16: error-semantics unification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breakpoint_trace_rejects_both_access_flags_false(monkeypatch):
    """A-2: read=False + write=False collapsed silently to a read-only
    trace — the caller's intent (change watch, or a typo) got no signal."""
    from ppsspp_dfx_mcp.tools import breakpoint as bp_mod
    from ppsspp_dfx_mcp.tools.breakpoint import breakpoint

    mock = _patch_tool_client(monkeypatch, bp_mod)
    with pytest.raises(ArgsInvalid, match="cannot both be false"):
        await breakpoint(
            action="trace",
            address="0x08804000",
            read=False,
            write=False,
            session_id="sess-1",
        )
    mock.mem_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_batch_step_unknown_type_is_step_invalid(monkeypatch):
    """A-16: unknown step type reported ARGS_INVALID while every other
    step-shape defect reported STEP_INVALID — callers branching on the
    error code got three verdicts for one class of problem."""
    from ppsspp_dfx_mcp.errors import StepInvalid
    from ppsspp_dfx_mcp.tools import batch_step as bs_mod
    from ppsspp_dfx_mcp.tools.batch_step import batch_step

    _patch_tool_client(monkeypatch, bs_mod)
    with pytest.raises(StepInvalid, match="invalid type"):
        await batch_step(
            session_id="s1",
            steps=[{"type": "bogus"}],
        )


# ---------------------------------------------------------------------------
# A-7 / A-9: read-side hardening (diff forms, analyze TOCTOU)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diff_snapshot_rejects_mixed_range_forms(monkeypatch):
    """A-7: start (without end) + address+size passed the partial-form
    check and `start` was silently dropped."""
    from ppsspp_dfx_mcp.tools import diff as diff_mod
    from ppsspp_dfx_mcp.tools.diff import diff_memory

    _patch_tool_client(monkeypatch, diff_mod)
    with pytest.raises(ArgsInvalid, match="ONE range form"):
        await diff_memory(
            action="snapshot",
            start="0x09000000",
            address="0x09000000",
            size=64,
            session_id="sess-1",
        )


def test_analyze_log_cap_rechecked_on_open_handle(monkeypatch, tmp_path):
    """A-9: stat-then-open TOCTOU — a file that grows between stat and
    read used to escape the MAX_LOG_BYTES cap. The authoritative check
    must be os.fstat on the open handle."""
    import os as _os
    import types

    from ppsspp_dfx_mcp.tools import analyze as analyze_mod

    calls = []

    real_fstat = _os.fstat

    def fake_fstat(fd):
        st = real_fstat(fd)
        calls.append(st.st_size)
        # The code only reads .st_size — a plain stand-in avoids stat_result
        # constructor constraints and puts the oversized size where it counts.
        return types.SimpleNamespace(st_size=analyze_mod.MAX_LOG_BYTES + 1)

    monkeypatch.setattr(analyze_mod.os, "fstat", fake_fstat)
    log = tmp_path / "ppsspp.log"
    log.write_text("ERROR line\n", encoding="utf-8")
    with pytest.raises(ArgsInvalid, match="too large"):
        analyze_mod._filter_log_lines(log, ["ERROR"])
    assert calls, "fstat must be consulted on the open handle"


def test_analyze_log_no_absolute_path_in_size_error(tmp_path, monkeypatch):
    """A-9 companion: the size error keeps the client-safe shape."""
    from ppsspp_dfx_mcp.tools import analyze as analyze_mod

    big = tmp_path / "big.log"
    big.write_bytes(b"x" * (analyze_mod.MAX_LOG_BYTES + 1))
    with pytest.raises(ArgsInvalid, match="too large"):
        analyze_mod._filter_log_lines(big, ["ERROR"])


# ---------------------------------------------------------------------------
# A-10 / A-11: protocol-shape robustness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_breakpoint_set_tolerates_hex_string_list_addresses(monkeypatch):
    """A-10: `int(bp['address'])` raised ValueError (→ INTERNAL) when a
    variant build echoed hex strings in breakpoint.list; a missing address
    silently fell back to the requested enabled — masking misreports."""

    from ppsspp_dfx_mcp.tools import breakpoint as bp_mod
    from ppsspp_dfx_mcp.tools.breakpoint import breakpoint

    mock = AsyncMock()
    mock.cpu_bp_add.return_value = {}
    mock.cpu_bp_list.return_value = {"breakpoints": [{"address": "0x08804000", "enabled": False}]}
    _patch_tool_client(monkeypatch, bp_mod, client=mock)
    result = await breakpoint(
        action="set", address="0x08804000", enabled=False, session_id="sess-1"
    )
    assert result["enabled"] is False  # echoed from the list, not the request


@pytest.mark.asyncio
async def test_replay_wait_complete_treats_missing_executing_as_unknown():
    """A-11: a status reply without `executing` used to read as 'completed'
    via the dict default — the wait returned success on a protocol surprise."""

    from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient

    class _NoExecutingTransport:
        async def call(self, event, timeout=None, **params):
            return {"event": event}  # never carries `executing`

    client = PpssppDebugClient(_NoExecutingTransport())
    with pytest.raises(TimeoutError, match="wait_complete"):
        await client.replay_wait_complete(timeout_ms=300, interval_ms=50)


# ---------------------------------------------------------------------------
# A-14 / A-15 / A-18: session & config hygiene
# ---------------------------------------------------------------------------


def test_save_sessions_cleans_up_tmp_on_failure(tmp_path, monkeypatch):
    """A-14: a failed dump/replace used to strand sessions.json.<pid>.tmp
    in the state dir; the file also had a world-readable window before the
    late chmod."""
    from ppsspp_dfx_mcp.session import session_manager as sm

    monkeypatch.setattr(sm, "sessions_path", lambda: tmp_path / "sessions.json")

    def boom(_payload, *a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(sm.json, "dump", boom)

    from ppsspp_dfx_mcp.models.session import Session as _S

    real = _S(session_id="s", iso_path="fake.iso")
    with pytest.raises(OSError):
        sm._save_sessions({"s": real})
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_sessions_success_leaves_no_tmp(tmp_path, monkeypatch):
    from ppsspp_dfx_mcp.session import session_manager as sm

    target = tmp_path / "sessions.json"
    monkeypatch.setattr(sm, "sessions_path", lambda: target)
    monkeypatch.setattr(sm, "_session_to_dict", lambda s: {"session_id": "s"})

    from ppsspp_dfx_mcp.models.session import Session as _S

    real = _S(session_id="s", iso_path="fake.iso")
    sm._save_sessions({"s": real})
    assert target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_gc_skips_session_touched_during_reclaim(monkeypatch):
    """A-15: a session touched between the phase-1 snapshot and the kill
    must NOT get its process killed underneath an in-flight tool call."""
    from datetime import UTC, datetime, timedelta

    from ppsspp_dfx_mcp.models.session import Session
    from ppsspp_dfx_mcp.session import session_manager as sm

    def mk_sess(idle: float) -> Session:
        return Session(
            session_id="s-touch",
            iso_path="fake.iso",
            pid=4242,
            last_active_at=datetime.now(UTC) - timedelta(seconds=idle),
        )

    snapshot = {"s-touch": mk_sess(sm.IDLE_GC_THRESHOLD_S + 10)}  # expired at scan
    fresh = {"s-touch": mk_sess(1.0)}  # touched before kill

    state = {"n": 0}

    async def fake_load():
        state["n"] += 1
        return snapshot if state["n"] == 1 else fresh

    async def fake_save(sessions):
        return None

    monkeypatch.setattr(sm, "_load_sessions_async", fake_load)
    monkeypatch.setattr(sm, "_save_sessions_async", fake_save)
    monkeypatch.setattr(sm.proc, "is_pid_alive", lambda pid: True)

    killed: list[int] = []

    async def fake_kill(pid):
        killed.append(pid)

    monkeypatch.setattr(sm, "_kill_stale_pid_safe", fake_kill)

    mgr = sm.get_session_manager()
    stopped = await mgr.gc_idle_sessions()
    assert stopped == []
    assert killed == []  # the touched session was never killed


def test_addresses_cached_until_file_changes(tmp_path, monkeypatch):
    """A-18: repeated calls must not re-read the YAML; an edit (mtime_ns+
    size key) must invalidate the cache."""
    from ppsspp_dfx_mcp import config as cfg

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    yaml_file = cfg_dir / "addresses.yaml"
    yaml_file.write_text("known_functions: {}\n", encoding="utf-8")
    monkeypatch.setenv(cfg._CONFIG_DIR_ENV, str(cfg_dir))
    cfg._ADDRESSES_CACHE = None

    calls = {"n": 0}
    real_load = cfg._load_yaml

    def counting_load(filename, default=None):
        calls["n"] += 1
        return real_load(filename, default=default)

    monkeypatch.setattr(cfg, "_load_yaml", counting_load)

    cfg.addresses()
    cfg.addresses()
    cfg.addresses()
    assert calls["n"] == 1, "identical repeat calls must hit the cache"

    import os as _os

    yaml_file.write_text("known_functions: {'f': 1}\n", encoding="utf-8")
    st = yaml_file.stat()
    _os.utime(yaml_file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    cfg.addresses()
    assert calls["n"] == 2, "an edited file must invalidate the cache"
