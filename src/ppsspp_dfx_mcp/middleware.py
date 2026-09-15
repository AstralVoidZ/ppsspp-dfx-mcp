"""Native middleware for the MCP SDK v2 dispatch layer: RequestId + RateLimit.

Registered on the server via the `MCPServer(middleware=[...])` constructor
parameter and runs at the protocol-dispatch tier (every inbound JSON-RPC
message, before params validation). Signature is the SDK's
`ServerMiddleware` protocol: `(ctx, call_next) -> HandlerResult`
(see `mcp.server.context`).

Status: the SDK marks the middleware chain provisional (may change in a
2.x minor). Per design_ppsspp_dfx_mcp_sdk_v2_rewrite_v1.md D5, these two
middlewares only observe or refuse — no business logic — so an SDK change
costs at most this one file.

Note: middleware wraps protocol-level dispatch. In-process calls that
bypass the wire protocol (e.g. `await mcp.call_tool(...)` in tests, or a
tool invoking another tool function directly) do NOT pass through it.
"""

from __future__ import annotations

import logging
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from functools import lru_cache
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
]

# Per-asyncio-task context variable for the current request_id.
_request_id_ctx: ContextVar[str] = ContextVar("ppsspp_dfx_request_id", default="-")


def get_request_id() -> str:
    """Return the current request_id, or '-' if no middleware is active."""
    return _request_id_ctx.get()


@runtime_checkable
class RateLimiter(Protocol):
    """Per-key rate limiting. Sync — pure in-memory arithmetic."""

    def acquire(self, key: str) -> None: ...


class TokenBucketLimiter:
    """Token-bucket rate limiter — per-key bucket (zero external deps)."""

    def __init__(self, calls_per_minute: float) -> None:
        self._rate = calls_per_minute
        self._buckets: dict[str, tuple[float, float]] = {}

    def acquire(self, key: str) -> None:
        if self._rate <= 0:
            return
        now = time.monotonic()
        # Lazy GC: prune buckets unused for > 1h once the table grows past
        # a threshold, so long-running servers don't leak memory per-tool.
        if len(self._buckets) > 100:
            self._buckets = {
                k: v for k, v in self._buckets.items() if now - v[1] < 3600
            }
        tokens, last = self._buckets.get(key, (self._rate, now))
        elapsed = now - last
        tokens = min(self._rate, tokens + elapsed * self._rate / 60.0)
        if tokens < 1.0:
            from ppsspp_dfx_mcp.errors import RateLimitExceeded
            raise RateLimitExceeded(
                f"rate limit {int(self._rate)}/min exceeded for tool {key!r}"
            )
        self._buckets[key] = (tokens - 1.0, now)


@lru_cache(maxsize=1)
def _get_limiter() -> RateLimiter:
    """Lazily construct the limiter.

    Deferred construction ensures `rate_limit()` reads the env var set by
    `__main__.py` CLI parsing (PPSSPP_DFX_RATE_LIMIT), not the value at
    module import time. Cached so subsequent calls reuse the same limiter.
    """
    return TokenBucketLimiter(rate_limit())


def _extract_tool_name(params: Any) -> str | None:
    """Extract the tool name from raw, pre-validation `tools/call` params.

    At the middleware tier `ctx.params` has NOT been model-validated yet,
    so it may be a plain dict (JSON-RPC path) or an already-typed model.
    Returns None when the shape is unrecognized (caller then skips
    limiting rather than refusing a well-formed request).
    """
    if params is None:
        return None
    if isinstance(params, dict):
        value = params.get("name")
        return value if isinstance(value, str) else None
    value = getattr(params, "name", None)
    return value if isinstance(value, str) else None


async def request_id_middleware(ctx: Any, call_next: Any) -> Any:
    """Inject a request_id into the module-level ContextVar.

    Uses the protocol request_id when present; generates a unique 8-char
    hex ID otherwise (notifications, or any context without an ID), so
    downstream log entries always carry a meaningful correlation id.
    """
    request_id = getattr(ctx, "request_id", None)
    _request_id_ctx.set(request_id if request_id else uuid4().hex[:8])
    try:
        return await call_next(ctx)
    finally:
        _request_id_ctx.set("-")


async def rate_limit_middleware(ctx: Any, call_next: Any) -> Any:
    """Per-tool token-bucket limit (refuse path for `tools/call` only).

    Raising `MCPError` instead of calling `call_next` turns the refusal
    into a JSON-RPC error while keeping the connection alive (SDK
    middleware "refuse" semantics). `RateLimitExceeded` carries the
    RATE_LIMIT_EXCEEDED code used by the error-code contract.
    """
    if getattr(ctx, "method", None) == "tools/call":
        tool_name = _extract_tool_name(getattr(ctx, "params", None))
        if tool_name:
            try:
                _get_limiter().acquire(tool_name)
            except Exception as exc:
                from mcp import MCPError

                from ppsspp_dfx_mcp.errors import RateLimitExceeded

                if isinstance(exc, RateLimitExceeded):
                    raise MCPError(
                        -32000,  # implementation-defined JSON-RPC range
                        str(exc),
                        data={"code": "RATE_LIMIT_EXCEEDED", "tool": tool_name},
                    ) from exc
                raise
    return await call_next(ctx)
