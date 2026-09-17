"""L4 regression tests for V001.

Violation:
- V001 [MEDIUM]: `read_string` had a `length` parameter that does not
  exist in the PPSSPP `memory.readString` contract, and was missing
  the `type` parameter. PPSSPP silently ignores unregistered params,
  so `length=None` was always ineffective. See MemorySubscriber.cpp:
  L38, L240-269 — only `address` (u32, required) and `type` (string,
  optional, "utf-8"|"base64", default "utf-8") are registered.

Fix: signature rewritten to `(address, encoding="utf-8")`. The
`length` parameter was removed; the `encoding` parameter (Python
side) was added with default "utf-8" and forwarded to the transport
as the WS event parameter `type` (the PPSSPP protocol name). The
Python-side name `encoding` avoids shadowing the built-in `type`.

Anchor:
- L4: `length` NOT in signature; `encoding` IS in signature with
  default "utf-8".
- L1: WS event params match `memory.readString` contract (forwarded
  under the `type` key).
"""

from __future__ import annotations

import inspect

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


class TestV001ReadStringSignature:
    """V001: read_string signature must match PPSSPP contract."""

    def test_length_param_removed_from_signature(self):
        """L4 anchor: `length` not in signature."""
        sig = inspect.signature(PpssppDebugClient.read_string)
        actual_params = set(sig.parameters.keys()) - {"self"}
        assert "length" not in actual_params, (
            "read_string signature still has `length` param. "
            "If this fails, V001 fix was reverted. See "
            "MemorySubscriber.cpp:L38, L240-269."
        )

    def test_encoding_param_present_with_utf8_default(self):
        """L4 anchor: `encoding` in signature with default 'utf-8'.

        Python 参数名为 `encoding`（避免遮蔽内置 type）；转发到
        PPSSPP WS 事件时仍使用协议参数名 `type`。
        """
        sig = inspect.signature(PpssppDebugClient.read_string)
        params = sig.parameters
        assert "encoding" in params, (
            "read_string signature missing `encoding` param. "
            "V001 fix requires encoding='utf-8' default."
        )
        assert params["encoding"].default == "utf-8", (
            f"read_string `encoding` default should be 'utf-8', got {params['encoding'].default!r}"
        )

    @pytest.mark.asyncio
    async def test_default_call_uses_bounded_read(self, client, transport):
        """L1 anchor (F-3 fix): read_string(0x08800000) issues a bounded
        memory.read (default cap 4096) — memory.readString is never used.
        """
        import base64 as _b64

        transport.set_response(
            "memory.read",
            {"base64": _b64.b64encode(b"hello").decode("ascii")},
        )
        result = await client.read_string(0x08800000)
        assert transport.calls[-1][0] == "memory.read"
        assert transport.calls[-1][1] == {
            "address": 0x08800000,
            "size": 4096,
        }
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_encoding_base64_returns_base64_payload(self, client, transport):
        """L1 anchor (F-3 fix): encoding='base64' returns the base64 of
        the bounded raw bytes (no NUL present).
        """
        import base64 as _b64

        transport.set_response(
            "memory.read",
            {"base64": _b64.b64encode(b"hello").decode("ascii")},
        )
        result = await client.read_string(0x08800000, encoding="base64")
        assert result == _b64.b64encode(b"hello").decode("ascii")
