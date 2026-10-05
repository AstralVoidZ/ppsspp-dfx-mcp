"""gpu_stats must name WHY a ticketed call timed out (T044 / C4.4).

D16 was diagnosed twice and got it wrong twice, because the tool reported
only "timed out" -- a symptom compatible with four different situations.
A caller who cannot tell them apart has to guess.

These tests pin that the attribution reaches the ERROR the agent sees, and
that it is absent (rather than wrong) when there is nothing to say.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest

from ppsspp_dfx_mcp.errors import WsTimeout


def _stub(client):
    @asynccontextmanager
    async def _cm(_sid):
        yield client

    return patch("ppsspp_dfx_mcp.tools.gpu_stats.session_client", _cm)


def _diag_holder(transport) -> object:
    return transport


class TestTimeoutCarriesCause:
    @pytest.mark.asyncio
    async def test_no_producer_cause_is_reported(self, client, transport) -> None:
        """D16's real signature: timed out, nothing ever broadcast."""
        from ppsspp_dfx_mcp.tools.gpu_stats import gpu_stats

        rec = transport.diagnostics.begin("gpu.stats.get", "t-1")
        transport.diagnostics.settle(rec, timed_out=True, error="handshake timeout", stepping=False)

        async def _boom(*a, **k):
            raise WsTimeout("gpu.stats.get timed out after 5.0s")

        client.gpu_stats = _boom  # type: ignore[attr-defined]
        with _stub(client), pytest.raises(WsTimeout) as exc:
            await gpu_stats(session_id="s")
        msg = str(exc.value)
        assert "no_producer" in msg, f"cause not attributed: {msg}"
        assert "saw_same_event_broadcast: False" in msg
        assert "next:" in msg, "the error must say what to DO, not just classify"

    @pytest.mark.asyncio
    async def test_pairing_broken_is_distinguished(self, client, transport) -> None:
        from ppsspp_dfx_mcp.tools.gpu_stats import gpu_stats

        rec = transport.diagnostics.begin("gpu.stats.get", "t-1")
        transport.diagnostics.note_broadcast("gpu.stats.get")
        transport.diagnostics.settle(rec, timed_out=True, error="no reply")

        async def _boom(*a, **k):
            raise WsTimeout("gpu.stats.get timed out after 5.0s")

        client.gpu_stats = _boom  # type: ignore[attr-defined]
        with _stub(client), pytest.raises(WsTimeout) as exc:
            await gpu_stats(session_id="s")
        msg = str(exc.value)
        assert "pairing_broken" in msg
        assert "saw_same_event_broadcast: True" in msg

    @pytest.mark.asyncio
    async def test_original_error_text_is_kept(self, client, transport) -> None:
        """The attribution is appended; it must not replace the cause."""
        from ppsspp_dfx_mcp.tools.gpu_stats import gpu_stats

        rec = transport.diagnostics.begin("gpu.stats.get", "t-1")
        transport.diagnostics.settle(rec, timed_out=True, error="x")

        async def _boom(*a, **k):
            raise WsTimeout("gpu.stats.get timed out after 5.0s")

        client.gpu_stats = _boom  # type: ignore[attr-defined]
        with _stub(client), pytest.raises(WsTimeout) as exc:
            await gpu_stats(session_id="s")
        assert "gpu.stats.get timed out after 5.0s" in str(exc.value)

    @pytest.mark.asyncio
    async def test_hint_names_the_blocking_dialog(self, client, transport) -> None:
        """The D16 lesson encoded: nothing else hinted at the dialog."""
        from ppsspp_dfx_mcp.tools.gpu_stats import gpu_stats

        rec = transport.diagnostics.begin("gpu.stats.get", "t-1")
        transport.diagnostics.settle(rec, timed_out=True, error="x")

        async def _boom(*a, **k):
            raise WsTimeout("timed out")

        client.gpu_stats = _boom  # type: ignore[attr-defined]
        with _stub(client), pytest.raises(WsTimeout) as exc:
            await gpu_stats(session_id="s")
        # Revised 2026-10-02 (T036): the dialog was D16 cause and the
        # backend pin fixed it; the hint must NOT send the reader there.
        msg = str(exc.value).lower()
        assert "not the same as" in msg and "render" in msg, (
            "the hint must separate no-producer-for-this-event from not-rendering"
        )
        # The class prefix must appear exactly ONCE -- re-wrapping with
        # str(exc) used to double it.
        assert msg.count("[ws_timeout]") == 1, f"double-prefixed: {msg!r}"


class TestSilenceWhenUnknown:
    @pytest.mark.asyncio
    async def test_no_diagnostics_leaves_the_error_clean(self, client, transport) -> None:
        """No record means no claim -- an empty explanation is noise."""
        from ppsspp_dfx_mcp.tools.gpu_stats import gpu_stats

        async def _boom(*a, **k):
            raise WsTimeout("gpu.stats.get timed out after 5.0s")

        client.gpu_stats = _boom  # type: ignore[attr-defined]
        with _stub(client), pytest.raises(WsTimeout) as exc:
            await gpu_stats(session_id="s")
        msg = str(exc.value)
        # translate_tool_errors prefixes the code, so compare the text part
        assert "gpu.stats.get timed out after 5.0s" in msg
        assert "cause:" not in msg, f"an unsourced error was embellished with a guess: {msg!r}"
        assert "next:" not in msg

    @pytest.mark.asyncio
    async def test_unrelated_records_are_not_borrowed(self, client, transport) -> None:
        """A read_memory timeout must not explain a gpu.stats timeout."""
        from ppsspp_dfx_mcp.tools.gpu_stats import gpu_stats

        rec = transport.diagnostics.begin("read_memory", "t-1")
        transport.diagnostics.settle(rec, timed_out=True, error="x")

        async def _boom(*a, **k):
            raise WsTimeout("gpu.stats.get timed out")

        client.gpu_stats = _boom  # type: ignore[attr-defined]
        with _stub(client), pytest.raises(WsTimeout) as exc:
            await gpu_stats(session_id="s")
        assert "no_producer" not in str(exc.value), (
            "attribution was taken from a different event's record"
        )

    @pytest.mark.asyncio
    async def test_diagnostics_failure_does_not_mask_the_timeout(self, client, transport) -> None:
        """The attribution must never replace the error it explains."""
        from ppsspp_dfx_mcp.tools import gpu_stats as mod

        # A diagnostics object that raises on access: the worst case the
        # helper has to survive.
        class _Hostile:
            @property
            def diagnostics(self):
                raise RuntimeError("diagnostics exploded")

        async def _boom(*a, **k):
            raise WsTimeout("gpu.stats.get timed out after 5.0s")

        client.gpu_stats = _boom  # type: ignore[attr-defined]
        client._transport = _Hostile()  # type: ignore[attr-defined]
        with _stub(client), pytest.raises(WsTimeout) as exc:
            await mod.gpu_stats(session_id="s")
        assert "timed out after 5.0s" in str(exc.value)


class TestSuccessPathUnchanged:
    @pytest.mark.asyncio
    async def test_success_returns_the_same_shape(self, client, transport) -> None:
        from ppsspp_dfx_mcp.tools.gpu_stats import gpu_stats

        transport.set_response("gpu.stats.get", {"fps": 60.0, "vblanks": 1})
        with _stub(client):
            out = await gpu_stats(session_id="s")
        assert out["fps"] == 60.0
        assert "raw" in out


class TestHelperIsDefensive:
    def test_missing_transport_returns_empty(self) -> None:
        from ppsspp_dfx_mcp.tools.gpu_stats import _attribution_suffix

        assert _attribution_suffix(object()) == ""

    def test_missing_diagnostics_returns_empty(self) -> None:
        from ppsspp_dfx_mcp.tools.gpu_stats import _attribution_suffix

        class _C:
            _transport = object()

        assert _attribution_suffix(_C()) == ""

    def test_exploding_diagnostics_returns_empty(self) -> None:
        from ppsspp_dfx_mcp.tools.gpu_stats import _attribution_suffix

        class _D:
            def last(self, _e=None):
                raise RuntimeError("boom")

        class _T:
            diagnostics = _D()

        class _C:
            _transport = _T()

        assert _attribution_suffix(_C()) == ""
