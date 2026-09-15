"""L4 regression tests for V002.

Violation:
- V002 [MEDIUM]: `read_bytes` had a `data` fallback
  (`resp.get("base64", "") or resp.get("data", "")`) that masked
  contract drift. PPSSPP `memory.read` only returns the `base64`
  field (string). See MemorySubscriber.cpp:L37, L193-227. The fallback
  to a non-existent `data` field could hide upstream protocol changes
  and silently return empty bytes when PPSSPP renamed the field.

Fix: removed the `or resp.get("data", "")` fallback. `read_bytes` now
reads only the `base64` field.

Anchor:
- L1: WS event `memory.read` returns a `base64` string field.
- L4: `read_bytes` source contains NO `data` fallback pattern.
"""

from __future__ import annotations

import base64 as b64
import inspect
from typing import Any

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


class TestV002ReadBytesNoDataFallback:
    """V002: read_bytes must read `base64` only (no `data` fallback)."""

    @pytest.mark.asyncio
    async def test_read_bytes_parses_base64_field(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """L1 anchor: `base64` field is decoded into bytes."""
        # "hello" base64-encoded
        encoded = b64.b64encode(b"hello").decode("ascii")
        transport.set_response("memory.read", {"base64": encoded})

        result = await client.read_bytes(address=0x08800000, size=5)

        assert result == b"hello"
        assert transport.calls[-1][0] == "memory.read"
        assert transport.calls[-1][1] == {
            "address": 0x08800000,
            "size": 5,
        }

    @pytest.mark.asyncio
    async def test_read_bytes_empty_base64_yields_empty_bytes(
        self, client: PpssppDebugClient, transport: Any
    ) -> None:
        """L1 anchor: empty `base64` returns b"", does NOT fall back to `data`.

        If the V002 fix is reverted (re-introducing `or resp.get("data", "")`),
        a response with both `base64=""` and `data="payload"` would silently
        return non-empty bytes — this test would still pass because no `data`
        key is set, but the source-level static check below catches the
        regression directly.
        """
        transport.set_response("memory.read", {"base64": ""})

        result = await client.read_bytes(address=0x08800000, size=4)

        assert result == b""

    def test_source_has_no_data_fallback(self) -> None:
        """L4 anchor: `read_bytes` source must not contain `data` fallback.

        Inspects the live source of `read_bytes`. If the V002 fix is
        reverted (re-introducing `resp.get("data", "")` or similar),
        this assertion fails immediately.
        """
        src = inspect.getsource(PpssppDebugClient.read_bytes)
        assert 'resp.get("data"' not in src, (
            "read_bytes must NOT contain a `resp.get(\"data\", ...)` "
            "fallback — V002 fix was reverted. See "
            "MemorySubscriber.cpp:L37, L193-227 (memory.read returns "
            "only the `base64` field)."
        )
        assert "or resp.get(\"data\"" not in src, (
            "read_bytes must NOT contain an `or resp.get(\"data\", ...)` "
            "fallback pattern — V002 fix was reverted."
        )
