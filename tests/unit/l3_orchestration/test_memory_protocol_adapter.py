"""L3 orchestration tests: memory/assemble protocol-adapter layer (batch 2).

Anchors D-01 + D-22 + D-13 (batch 2 protocol-adapter fixes):
- D-01: read_string max_len > 0 uses read_bytes + local NUL scan instead
  of PPSSPP memory.readString (which strnlen-scans 32MB → timeout).
- D-22: assemble splits multi-instruction input on '\n' and ';', calls
  client.assemble once per instruction, incrementing address by 4 each
  time (PPSSPP only assembles the first instruction for multi-input).
- D-13: scan pattern_type='ascii' encodes the pattern string as ASCII
  bytes (pattern_type='hex' default uses existing _decode_hex_pattern).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from ppsspp_dfx_mcp.tools.assemble import _split_instructions, assemble
from ppsspp_dfx_mcp.tools.memory import read_memory

# ============================================================================
# D-01: read_string max_len fallback to read_bytes
# ============================================================================


class TestReadStringMaxLenFallback:
    """L3: read_string(max_len>0) uses read_bytes + local NUL scan (D-01).

    PPSSPP memory.readString uses strnlen scanning 32MB → timeout on
    strings without NUL terminator. When max_len > 0, the tool uses
    read_bytes(address, max_len) and finds NUL locally, bounding the
    read to max_len bytes.
    """

    @pytest.mark.asyncio
    async def test_max_len_forwards_cap_to_client_read_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """max_len=256 forwards the cap to client.read_string (F-3)."""
        mock_client = AsyncMock()
        mock_client.read_string.return_value = "hello"

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        result = await read_memory(
            action="read_string",
            address=0x08804000,
            max_len=256,
            session_id="sess-1",
        )

        # F-3 fix: the tool routes through the bounded client.read_string
        # (which itself does read_bytes + local NUL scan) — the dangerous
        # PPSSPP memory.readString event is never used. max_len=256 is
        # forwarded as the cap.
        mock_client.read_string.assert_awaited_once_with(address=0x08804000, max_length=256)
        # Value is truncated at NUL.
        assert result["value"] == "hello"

    @pytest.mark.asyncio
    async def test_max_len_zero_uses_default_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """max_len=0 (default) uses the 4096-byte default cap (F-3 fix).

        The unbounded PPSSPP memory.readString path no longer exists.
        """
        mock_client = AsyncMock()
        mock_client.read_string.return_value = "hello"

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        result = await read_memory(
            action="read_string",
            address=0x08804000,
            session_id="sess-1",
        )

        mock_client.read_string.assert_awaited_once_with(address=0x08804000, max_length=4096)
        assert result["value"] == "hello"

    @pytest.mark.asyncio
    async def test_max_len_no_nul_returns_full_buffer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No NUL in buffer → returns full max_len bytes decoded."""
        mock_client = AsyncMock()
        mock_client.read_string.return_value = "abcdef"  # no NUL, capped decode

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        result = await read_memory(
            action="read_string",
            address=0x08804000,
            max_len=6,
            session_id="sess-1",
        )

        mock_client.read_string.assert_awaited_once_with(address=0x08804000, max_length=6)
        assert result["value"] == "abcdef"
        assert result["size"] == 6


# ============================================================================
# D-22: assemble multi-instruction splitting
# ============================================================================


class TestAssembleMultiInstructionSplit:
    """L3: assemble splits on '\n' and ';', calls per instruction (D-22).

    PPSSPP memory.assemble only assembles the first instruction for
    multi-instruction input. The tool splits and calls once per
    instruction, incrementing address by 4 (MIPS fixed width).
    """

    def test_split_semicolon(self):
        """_split_instructions splits on ';'."""
        assert _split_instructions("nop; addiu r5, r0, 1; jr ra") == [
            "nop",
            "addiu r5, r0, 1",
            "jr ra",
        ]

    def test_split_newline(self):
        """_split_instructions splits on '\\n'."""
        assert _split_instructions("nop\naddiu r5, r0, 1\njr ra") == [
            "nop",
            "addiu r5, r0, 1",
            "jr ra",
        ]

    def test_split_mixed(self):
        """_split_instructions handles mixed ';' and '\\n'."""
        assert _split_instructions("nop; addiu r5, r0, 1\njr ra") == [
            "nop",
            "addiu r5, r0, 1",
            "jr ra",
        ]

    def test_split_strips_whitespace(self):
        """_split_instructions strips whitespace from each fragment."""
        assert _split_instructions("  nop  ;  addiu r5  ") == ["nop", "addiu r5"]

    def test_split_drops_empty(self):
        """_split_instructions drops empty fragments."""
        assert _split_instructions("nop;;\n;addiu") == ["nop", "addiu"]

    @pytest.mark.asyncio
    async def test_multi_instruction_calls_assemble_per_instruction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3 instructions → 3 client.assemble calls, address += 4 each."""
        mock_client = AsyncMock()
        mock_client.assemble.return_value = {"encoding": 0x00000000}

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.assemble.session_client",
            fake_session_client,
        )

        # N-04: use a non-protected address (0x09000000) so the
        # assemble tool's protected-address check does not reject the
        # call. Pre-N-04, assemble bypassed the check and any address
        # worked; now the test must use a writable address or pass
        # force=True. We use a non-protected address to keep this test
        # focused on multi-instruction splitting (not protection).
        result = await assemble(
            session_id="sess-1",
            address=0x09000000,
            code="nop; addiu r5, r0, 1; jr ra",
        )

        assert mock_client.assemble.await_count == 3
        # Verify addresses increment by 4.
        calls = mock_client.assemble.call_args_list
        assert calls[0].kwargs["address"] == 0x09000000
        assert calls[1].kwargs["address"] == 0x09000004
        assert calls[2].kwargs["address"] == 0x09000008
        # Verify each instruction is forwarded separately.
        assert calls[0].kwargs["code"] == "nop"
        assert calls[1].kwargs["code"] == "addiu r5, r0, 1"
        assert calls[2].kwargs["code"] == "jr ra"
        # bytes_written = 3 * 4 = 12.
        assert result["bytes_written"] == 12

    @pytest.mark.asyncio
    async def test_single_instruction_one_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Single instruction (no separator) → 1 client.assemble call."""
        mock_client = AsyncMock()
        mock_client.assemble.return_value = {"encoding": 0x00000000}

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.assemble.session_client",
            fake_session_client,
        )

        # N-04: use non-protected address (see multi-instruction test
        # above for rationale).
        result = await assemble(
            session_id="sess-1",
            address=0x09000000,
            code="nop",
        )

        assert mock_client.assemble.await_count == 1
        assert result["bytes_written"] == 4


# ============================================================================
# D-13: scan pattern_type parameter
# ============================================================================


class TestScanPatternType:
    """L3: scan pattern_type='ascii' encodes pattern as ASCII bytes (D-13).

    pattern_type='hex' (default) uses existing _decode_hex_pattern.
    pattern_type='ascii' calls pattern.encode('ascii').
    """

    @pytest.mark.asyncio
    async def test_ascii_pattern_type_encodes_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """pattern_type='ascii' encodes 'hello' as b'hello'."""
        mock_client = AsyncMock()
        mock_client.scan_memory.return_value = []

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        await read_memory(
            action="scan",
            pattern="hello",
            pattern_type="ascii",
            start_addr=0x08800000,
            end_addr=0x08900000,
            session_id="sess-1",
        )

        # Verify scan_memory received ASCII-encoded bytes.
        call_kwargs = mock_client.scan_memory.call_args.kwargs
        assert call_kwargs["pattern"] == b"hello", (
            "pattern_type='ascii' must encode 'hello' as b'hello' — not hex-decode it."
        )

    @pytest.mark.asyncio
    async def test_hex_pattern_type_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """pattern_type='hex' (default) hex-decodes the pattern."""
        mock_client = AsyncMock()
        mock_client.scan_memory.return_value = []

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        await read_memory(
            action="scan",
            pattern="AABB",
            start_addr=0x08800000,
            end_addr=0x08900000,
            session_id="sess-1",
        )

        call_kwargs = mock_client.scan_memory.call_args.kwargs
        assert call_kwargs["pattern"] == b"\xaa\xbb", (
            "Default pattern_type='hex' must hex-decode 'AABB' as b'\\xAA\\xBB'."
        )

    @pytest.mark.asyncio
    async def test_ascii_pattern_type_invalid_hex_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pattern_type='ascii' with non-hex string works (no hex decode)."""
        mock_client = AsyncMock()
        mock_client.scan_memory.return_value = []

        @asynccontextmanager
        async def fake_session_client(
            session_id: str,
        ) -> AsyncIterator[AsyncMock]:
            yield mock_client

        monkeypatch.setattr(
            "ppsspp_dfx_mcp.tools.memory.session_client",
            fake_session_client,
        )

        # "jr ra" is not valid hex — would fail with pattern_type='hex',
        # but works with pattern_type='ascii'.
        await read_memory(
            action="scan",
            pattern="jr ra",
            pattern_type="ascii",
            start_addr=0x08800000,
            end_addr=0x08900000,
            session_id="sess-1",
        )

        call_kwargs = mock_client.scan_memory.call_args.kwargs
        assert call_kwargs["pattern"] == b"jr ra"
