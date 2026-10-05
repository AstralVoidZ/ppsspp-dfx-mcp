"""W16 + A11/W32 regression guards: rate-limit buckets and request-id.

Pre-fix defects this file locks out:

* Buckets were keyed by the raw, UNVALIDATED `name` string only, so (a) any
  string created a bucket and the table grew within the TTL window, (b) all
  sessions shared one bucket per tool name (session A could rate-limit B),
  (c) unrecognized params / empty name skipped limiting entirely.
* `request_id_middleware` finished with `_request_id_ctx.set("-")`, so a
  nested dispatch inside one asyncio task wiped the OUTER id.
* `middleware.get_request_id()` had no consumers: no log record carried it.

Each test is written to be able to fail (see the accompanying mutation notes
in the task report).
"""

from __future__ import annotations

import asyncio
import logging

import pytest
from mcp import MCPError

from ppsspp_dfx_mcp import config
from ppsspp_dfx_mcp import middleware as mw
from ppsspp_dfx_mcp.errors import RateLimitExceeded
from ppsspp_dfx_mcp.logging import PPSSPP_LOG_LOGGER_NAME, RequestIdFilter

_READ = "ppsspp_read_memory"


class _Ctx:
    """Minimal protocol-dispatch context (method/params/request_id)."""

    def __init__(self, *, method: str = "tools/call", params=None, request_id: str = "") -> None:
        self.method = method
        self.params = params
        self.request_id = request_id


def _install_known_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mw, "_known_tool_names", lambda: frozenset({_READ}))


def test_two_sessions_do_not_share_a_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Session A exhausting its budget must not rate-limit session B."""
    _install_known_tools(monkeypatch)
    limiter = mw.TokenBucketLimiter(1)  # one call/minute
    monkeypatch.setattr(mw, "_get_limiter", lambda: limiter)

    ctx_a = _Ctx(params={"name": _READ, "session_id": "sess-A"})
    ctx_b = _Ctx(params={"name": _READ, "session_id": "sess-B"})

    async def run() -> None:
        async def call_next(_ctx):
            return "ok"

        await mw.rate_limit_middleware(ctx_a, call_next)  # A's first call
        await mw.rate_limit_middleware(ctx_b, call_next)  # B has its own budget
        with pytest.raises(MCPError):
            await mw.rate_limit_middleware(ctx_a, call_next)  # A is now empty

    asyncio.run(run())


def test_garbage_tool_names_do_not_grow_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unregistered names collapse into one fixed bucket, not N buckets."""
    _install_known_tools(monkeypatch)
    limiter = mw.TokenBucketLimiter(100_000)
    for i in range(50):
        limiter.acquire(mw._bucket_key({"name": f"garbage_{i}", "session_id": "A"}))
    assert set(limiter._buckets) == {("__unknown__", "__unknown__")}


def test_empty_name_uses_unknown_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty/absent tool name fails closed into the unknown bucket."""
    _install_known_tools(monkeypatch)
    assert mw._bucket_key({"name": "", "session_id": "A"}) == ("__unknown__", "__unknown__")


def test_unknown_params_shape_is_still_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    """Malformed params must not bypass limiting (fail closed)."""
    _install_known_tools(monkeypatch)
    limiter = mw.TokenBucketLimiter(1)
    monkeypatch.setattr(mw, "_get_limiter", lambda: limiter)
    ctx = _Ctx(params=None)  # unrecognized shape

    async def run() -> None:
        async def call_next(_ctx):
            return "ok"

        await mw.rate_limit_middleware(ctx, call_next)
        with pytest.raises(MCPError):
            await mw.rate_limit_middleware(ctx, call_next)

    asyncio.run(run())


def test_prune_never_evicts_the_active_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pruning stale entries must keep the requested bucket and not rebuild."""
    limiter = mw.TokenBucketLimiter(1)
    now = mw.time.monotonic()
    stale = ("old-session", _READ)
    active = ("new-session", _READ)
    # Seed a stale bucket (idle > TTL) plus the active one.
    limiter._buckets[stale] = (0.0, now - mw._BUCKET_TTL_S - 1)
    limiter._buckets[active] = (0.5, now)
    for i in range(mw._PRUNE_THRESHOLD + 5):
        limiter._buckets[(f"bulk-{i}", _READ)] = (1.0, now)
    # Force the time gate open and prune while `active` has debt.
    limiter._last_prune = now - mw._PRUNE_INTERVAL_S - 1
    limiter._prune(now, keep=active)
    assert stale not in limiter._buckets, "stale bucket should be dropped"
    assert active in limiter._buckets, "the active bucket must survive pruning"
    assert limiter._buckets[active][1] == now, "active debt/last-use preserved"


def test_nested_dispatch_restores_outer_request_id() -> None:
    """A nested middleware must restore, not wipe, the outer request id."""
    seen: list[str] = []

    async def inner(_ctx) -> None:
        seen.append(mw.get_request_id())

    async def outer(_ctx) -> None:
        await mw.request_id_middleware(_Ctx(request_id="inner"), inner)
        seen.append(mw.get_request_id())

    asyncio.run(mw.request_id_middleware(_Ctx(request_id="outer"), outer))
    assert seen == ["inner", "outer"]
    assert mw.get_request_id() == "-", "must reset to the default after dispatch"


def test_configure_logging_handler_attaches_request_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A record emitted inside a dispatch carries the current request_id."""
    monkeypatch.setattr(config, "output_dir", lambda: tmp_path)
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    ppsspp_logger = logging.getLogger(PPSSPP_LOG_LOGGER_NAME)
    saved_ppsspp = ppsspp_logger.handlers[:]
    try:
        config.configure_logging()
        handler = root.handlers[0]
        assert any(isinstance(f, RequestIdFilter) for f in handler.filters)

        captured: dict[str, logging.LogRecord] = {}

        async def call_next(_ctx):
            record = logging.LogRecord("t.w32", logging.INFO, __file__, 1, "inside", None, None)
            handler.handle(record)
            captured["record"] = record

        asyncio.run(mw.request_id_middleware(_Ctx(request_id="req-xyz"), call_next))
        assert captured["record"].request_id == "req-xyz"
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        ppsspp_logger.handlers[:] = saved_ppsspp


def test_force_override_limiter_message_names_tool_and_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal text keeps the tool name and now names the session too."""
    _install_known_tools(monkeypatch)
    limiter = mw.TokenBucketLimiter(1)
    key = ("sess-A", _READ)
    limiter.acquire(key)
    with pytest.raises(RateLimitExceeded) as exc:
        limiter.acquire(key)
    assert _READ in str(exc.value)
    assert "sess-A" in str(exc.value)
