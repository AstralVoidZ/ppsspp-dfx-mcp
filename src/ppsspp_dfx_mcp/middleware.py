"""Native middleware for the MCP SDK v2 dispatch layer: RequestId + RateLimit.

Registered on the server via the `MCPServer(middleware=[...])` constructor
parameter and runs at the protocol-dispatch tier (every inbound JSON-RPC
message, before params validation). Signature is the SDK's
`ServerMiddleware` protocol: `(ctx, call_next) -> HandlerResult`
(see `mcp.server.context`).

Status: the SDK marks the middleware chain provisional (may change in a
2.x minor). Per the SDK v2 rewrite design, these two
middlewares only observe or refuse — no business logic — so an SDK change
costs at most this one file.

Note: middleware wraps protocol-level dispatch ONLY. In-process calls that
bypass the wire protocol (e.g. `await mcp.call_tool(...)` in tests, or a
tool invoking another tool function directly) do NOT pass through it, so
the rate limiter does NOT limit them — it is not a process-wide throttle.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import uuid4

from ppsspp_dfx_mcp.config import rate_limit

if TYPE_CHECKING:
    from mcp.server.context import CallNext, HandlerResult, ServerMiddleware  # noqa: F401

logger = logging.getLogger(__name__)

__all__ = [
    "RateLimiter",
    "TokenBucketLimiter",
    "get_request_id",
    "rate_limit_middleware",
    "request_id_middleware",
    "tool_error_middleware",
]

# Per-asyncio-task context variable for the current request_id.
_request_id_ctx: ContextVar[str] = ContextVar("ppsspp_dfx_request_id", default="-")


def get_request_id() -> str:
    """Return the current request_id, or '-' if no middleware is active."""
    return _request_id_ctx.get()


@runtime_checkable
class RateLimiter(Protocol):
    """Per-key rate limiting. Sync — pure in-memory arithmetic."""

    def acquire(self, key: tuple[str, str]) -> None: ...


# Bucket key sentinel for a request whose params shape is unrecognized or
# whose tool name is unknown/unregistered. Requests that cannot be
# attributed fail CLOSED into this single shared bucket instead of skipping
# the limit (the pre-fix behavior let garbage params bypass limiting).
_UNKNOWN_KEY = "__unknown__"
# Bucket key sentinel for a `tools/call` with no usable session id.
_GLOBAL_KEY = "__global__"

# Lazy-GC tuning: prune only when the table exceeds this size, and at most
# once per interval. The old code rebuilt the whole dict on EVERY acquire
# past 100 entries (an O(n) allocation per call).
_PRUNE_THRESHOLD = 100
_PRUNE_INTERVAL_S = 60.0
# Entries idle longer than this are stale and eligible for pruning.
_BUCKET_TTL_S = 3600.0


class TokenBucketLimiter:
    """Token-bucket rate limiter — per-(session, tool) bucket (no deps)."""

    def __init__(self, calls_per_minute: float) -> None:
        self._rate = calls_per_minute
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}
        self._last_prune = time.monotonic()

    def acquire(self, key: tuple[str, str]) -> None:
        if self._rate <= 0:
            return
        now = time.monotonic()
        self._prune(now, keep=key)
        tokens, last = self._buckets.get(key, (self._rate, now))
        elapsed = now - last
        tokens = min(self._rate, tokens + elapsed * self._rate / 60.0)
        if tokens < 1.0:
            from ppsspp_dfx_mcp.errors import RateLimitExceeded

            raise RateLimitExceeded(
                f"rate limit {int(self._rate)}/min exceeded for tool {key[1]!r} "
                f"(session {key[0]!r})"
            )
        self._buckets[key] = (tokens - 1.0, now)

    def _prune(self, now: float, *, keep: tuple[str, str]) -> None:
        """Drop stale buckets in place — never the bucket being used now.

        Gated on BOTH a size threshold and a time interval so pruning is not
        an O(n) rebuild on every acquire. `keep` is excluded so the key this
        call is about to use can never be evicted (the old prune ran before
        the `get` and could reset an active key's accumulated debt).
        """
        if len(self._buckets) <= _PRUNE_THRESHOLD:
            return
        if now - self._last_prune < _PRUNE_INTERVAL_S:
            return
        self._last_prune = now
        stale = [k for k, v in self._buckets.items() if k != keep and now - v[1] >= _BUCKET_TTL_S]
        for k in stale:
            del self._buckets[k]


@lru_cache(maxsize=1)
def _get_limiter() -> RateLimiter:
    """Lazily construct the limiter.

    Deferred construction ensures `rate_limit()` reads the env var set by
    `__main__.py` CLI parsing (PPSSPP_DFX_RATE_LIMIT), not the value at
    module import time. Cached so subsequent calls reuse the same limiter.
    """
    return TokenBucketLimiter(rate_limit())


def _known_tool_names() -> frozenset[str]:
    """Tool names registered on the server (lazy import).

    `server` imports this module at module scope, so importing it here must
    be lazy to avoid a cycle. Queried live (not cached) because dynamic
    `ppsspp_script_*` tools are added/removed at runtime.
    """
    from ppsspp_dfx_mcp.registry import registered_tool_names

    return frozenset(registered_tool_names())


def _extract_tool_name(params: Any) -> str | None:
    """Extract the tool name from raw, pre-validation `tools/call` params.

    At the middleware tier `ctx.params` has NOT been model-validated yet,
    so it may be a plain dict (JSON-RPC path) or an already-typed model.
    Returns None when the shape is unrecognized or the name is not a string
    — the caller then fails CLOSED into the shared unknown bucket.
    """
    if params is None:
        return None
    if isinstance(params, dict):
        value = params.get("name")
        return value if isinstance(value, str) else None
    value = getattr(params, "name", None)
    return value if isinstance(value, str) else None


def _extract_session_id(params: Any) -> str | None:
    """Extract the session id from raw `tools/call` params, if present."""
    if params is None:
        return None
    if isinstance(params, dict):
        value = params.get("session_id")
    else:
        value = getattr(params, "session_id", None)
    return value if isinstance(value, str) and value else None


def _extract_arguments(params: Any) -> dict[str, Any]:
    """Extract the raw `arguments` mapping from pre-validation `tools/call` params.

    `ctx.params` is the wire dict (or an already-typed model); `arguments`
    is absent for a no-arg call and may be `None`. Returns `{}` for
    anything that is not a mapping so `validate_arguments` sees the same
    empty-args case the SDK would.
    """
    if params is None:
        return {}
    value = (
        params.get("arguments") if isinstance(params, dict) else getattr(params, "arguments", None)
    )
    return value if isinstance(value, dict) else {}


def _lookup_tool(tool_name: str) -> Any:
    """Look up a registered tool's SDK `Tool` object (lazy import, no cycle)."""
    from ppsspp_dfx_mcp.registry import mcp

    return mcp._tool_manager._tools.get(tool_name)  # noqa: SLF001 — private SDK API, isolated here


def _format_args_invalid(exc: Any) -> str:
    """Render a pydantic `ValidationError` as `[ARGS_INVALID] <field>: <msg>`.

    Pydantic's own `str(exc)` is a multi-line dump carrying the internal
    model class name (`<tool>Arguments`) and an `errors.pydantic.dev` URL
    — unusable for a model caller. `errors()` gives us the same information
    field by field, so we rebuild it into one actionable line (FR-002).
    """
    parts: list[str] = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        parts.append(f"{field}: {err.get('msg', 'invalid value')}")
    return "[ARGS_INVALID] " + "; ".join(parts)


async def tool_error_middleware(ctx: Any, call_next: Any) -> Any:
    """Normalize tool-argument validation failures (FR-002 / G-2).

    The SDK validates `tools/call` arguments INSIDE `FunctionTool.run()`
    (`.venv/.../mcpserver/tools/base.py`), *after* this middleware chain,
    and raises `ToolError(f"Error executing tool {name}: {pydantic_dump}")`
    — the project's `@translate_tool_errors` only wraps the function body,
    so it never sees that layer. This middleware validates the raw
    arguments up front (same `fn_metadata.validate_arguments` the SDK
    calls) and, on failure, SHORT-CIRCUITS with an `[ARGS_INVALID]`
    result instead of letting the raw pydantic dump reach the client.

    Runs for every `tools/call` (static + dynamic `ppsspp_script_*`), so a
    new tool inherits the contract automatically. Validation is side-effect
    free (spike-3: no tool arg model uses `default_factory`); the result is
    discarded and the SDK re-validates on the success path. Unknown tools
    and non-`tools/call` methods fall through unchanged, so the SDK still
    owns METHOD_NOT_FOUND and every other error shape.
    """
    if getattr(ctx, "method", None) != "tools/call":
        return await call_next(ctx)
    params = getattr(ctx, "params", None)
    tool_name = _extract_tool_name(params)
    if not tool_name:
        return await call_next(ctx)
    tool = _lookup_tool(tool_name)
    if tool is None:
        return await call_next(ctx)

    from pydantic import ValidationError

    try:
        tool.fn_metadata.validate_arguments(_extract_arguments(params))
    except ValidationError as exc:
        from mcp.types import CallToolResult, TextContent

        return CallToolResult(
            content=[TextContent(type="text", text=_format_args_invalid(exc))],
            is_error=True,
        )
    return await call_next(ctx)


def _bucket_key(params: Any) -> tuple[str, str]:
    """The rate-limit bucket for a `tools/call`.

    Keyed by ``(session_id or "__global__", tool_name)`` so one session
    cannot exhaust another session's budget for the same tool. Only
    registered tool names create real buckets (attacker-chosen names would
    otherwise grow the table); anything unrecognized — wrong params shape,
    empty name, unregistered name — collapses into the fixed
    ``("__unknown__", "__unknown__")`` key, which IS limited (fail closed).
    """
    tool = _extract_tool_name(params)
    if not tool or tool not in _known_tool_names():
        return (_UNKNOWN_KEY, _UNKNOWN_KEY)
    session = _extract_session_id(params)
    return (session or _GLOBAL_KEY, tool)


async def request_id_middleware(ctx: Any, call_next: Any) -> Any:
    """Inject a request_id into the module-level ContextVar.

    Uses the protocol request_id when present; generates a unique 8-char
    hex ID otherwise (notifications, or any context without an ID), so
    downstream log entries always carry a meaningful correlation id.

    Uses a ContextVar token + ``reset`` (not ``set("-")``) so a middleware
    nested inside this one restores the OUTER id on exit instead of wiping
    it — nested dispatch happens within a single asyncio task.
    """
    request_id = getattr(ctx, "request_id", None)
    token = _request_id_ctx.set(request_id if request_id else uuid4().hex[:8])
    try:
        return await call_next(ctx)
    finally:
        _request_id_ctx.reset(token)


async def rate_limit_middleware(ctx: Any, call_next: Any) -> Any:
    """Per-(session, tool) token-bucket limit (refuse path, `tools/call`).

    Raising `MCPError` instead of calling `call_next` turns the refusal
    into a JSON-RPC error while keeping the connection alive (SDK
    middleware "refuse" semantics). `RateLimitExceeded` carries the
    RATE_LIMIT_EXCEEDED code used by the error-code contract.

    Only protocol dispatch is covered; in-process `mcp.call_tool(...)` and
    direct tool-to-tool calls never reach this function (see module doc).
    """
    if getattr(ctx, "method", None) == "tools/call":
        key = _bucket_key(getattr(ctx, "params", None))
        try:
            _get_limiter().acquire(key)
        except Exception as exc:
            from mcp import MCPError

            from ppsspp_dfx_mcp.errors import RateLimitExceeded

            if isinstance(exc, RateLimitExceeded):
                raise MCPError(
                    -32000,  # implementation-defined JSON-RPC range
                    str(exc),
                    data={"code": "RATE_LIMIT_EXCEEDED", "tool": key[1]},
                ) from exc
            raise
    return await call_next(ctx)
