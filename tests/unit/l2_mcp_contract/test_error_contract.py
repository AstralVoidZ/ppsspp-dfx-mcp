"""R1 (design_ppsspp_dfx_mcp_test_refactor_v1 §R1): full-stack error contract.

Drives ``MCPServer.call_tool()`` (the SDK v2 request handler that real
MCP clients talk to) and asserts, for every registered error path, that:

1. the result is ``is_error=True`` (never an exception escape, never a
   silent success), and
2. the client-visible text contains the BUSINESS message fragment —
   not the generic ``"Error executing tool <name>"`` crash wrapper.

Regression anchor: verification report F-1 (custom ToolError did not
subclass the SDK ToolError, so every business error reached clients as
a generic crash message) and F-11 (unknown-section / empty-match were
silent successes).

These tests need no PPSSPP and no live session: every scenario either
fails on argument validation or on session resolution.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError as SDKToolError
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from ppsspp_dfx_mcp import server as server_mod

pytestmark = pytest.mark.asyncio

# (tool, args, expected message fragment visible to the MCP client)
ERROR_MATRIX: list[tuple[str, dict, str]] = [
    # ── session lifecycle ────────────────────────────────────────────────
    ("ppsspp_session", {"action": "get"}, "session_id is required when action=get"),
    ("ppsspp_session", {"action": "stop"}, "session_id is required when action=stop"),
    ("ppsspp_session", {"action": "start"}, "iso_path is required when action=start"),
    ("ppsspp_session", {"action": "get", "session_id": "sess_missing"}, "session not found"),
    (
        "ppsspp_session",
        {"action": "start", "iso_path": "Z:/definitely/not/real.iso"},
        "ISO file not found",
    ),
    # ── memory ───────────────────────────────────────────────────────────
    # Schema Literal rejects unknown actions before the tool runs — the
    # client sees the pydantic literal_error text (R2 pins enum agreement).
    ("ppsspp_read_memory", {"action": "read_u64", "address": "0x08804000"}, "Input should be"),
    (
        "ppsspp_read_memory",
        {"action": "read_bytes", "address": "0x08804000", "size": 4, "session_id": "sess_missing"},
        "session not found",
    ),
    # ── aggregate actions (schema-Literal ↔ runtime enum agreement) ─────
    ("ppsspp_query", {"action": "teleport", "session_id": "sess_missing"}, "Input should be"),
    (
        "ppsspp_query",
        {"action": "register", "session_id": "sess_missing"},
        "requires a register name",
    ),
    ("ppsspp_step", {"action": "warp", "session_id": "sess_missing"}, "Input should be"),
    ("ppsspp_breakpoint", {"action": "explode", "session_id": "sess_missing"}, "Input should be"),
    ("ppsspp_replay", {"action": "rewind", "session_id": "sess_missing"}, "Input should be"),
    (
        "ppsspp_state_observer",
        {"action": "teleport", "session_id": "sess_missing"},
        "Input should be",
    ),
    # ── address / input validation ───────────────────────────────────────
    # (ppsspp_convert_address row removed in v0.1.6 — tool un-tooled)
    # ── config lookups (F-11: must fail loudly, not return empty) ───────
    ("ppsspp_list_addresses", {"section": "no_such_section"}, "unknown section"),
    (
        "ppsspp_search_memory_info",
        {"match": "", "session_id": "sess_missing"},
        "match must be a non-empty substring",
    ),
]


@pytest.fixture(scope="module", autouse=True)
def _registered():
    """Ensure all static tools are registered before the matrix runs."""
    server_mod.register_all_tools()


def _text_of(result) -> str:
    parts = getattr(result, "content", None) or []
    return "\n".join(getattr(c, "text", "") for c in parts)


async def _client_view(tool: str, args: dict) -> tuple[bool, str]:
    """Emulate the SDK request-handler classification boundary.

    ``MCPServer.call_tool`` is the *internal* path — it raises. The wire
    behavior is defined by the request handler (mcpserver/server.py
    ``_handle_call_tool``): an anticipated ToolError becomes
    ``is_error=True`` with ``str(exc)`` as text; anything else becomes a
    crash (``UnexpectedToolError``) whose client text is only
    ``"Error executing tool <name>"``. This helper reproduces exactly
    that mapping so tests assert what a real MCP client receives.
    """
    try:
        result = await server_mod.mcp.call_tool(tool, dict(args), None)
    except UnexpectedToolError as e:
        return True, str(e)
    except SDKToolError as e:
        return True, str(e)
    err = getattr(result, "is_error", None) or getattr(result, "isError", False)
    return bool(err), _text_of(result)


class TestFullStackErrorContract:
    """Every error path is isError=True with the business message intact."""

    @pytest.mark.parametrize(("tool", "args", "fragment"), ERROR_MATRIX)
    async def test_error_path_is_anticipated_and_readable(
        self, tool: str, args: dict, fragment: str
    ) -> None:
        is_error, text = await _client_view(tool, args)
        assert is_error, f"{tool} {args} must surface as isError=True"
        assert fragment in text, (
            f"{tool} {args}: client must see the business message "
            f"({fragment!r}); got {text[:200]!r} — F-1 regression"
        )
        # A CRASH (pre-F-1) hid everything behind exactly this string; an
        # anticipated error keeps the business message after the prefix.
        generic_crash = f"Error executing tool {tool}"
        assert text.strip() != generic_crash, (
            f"{tool} {args}: generic crash wrapper leaked — F-1 regression"
        )

    async def test_health_is_positive_control(self) -> None:
        """Sanity: the stack itself works — health returns isError=False."""
        is_error, _ = await _client_view("ppsspp_health", {})
        assert is_error is False
