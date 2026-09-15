"""L4 regression tests for V014.

Violation:
- V014 [HIGH]: `gpu_record_dump` docstring claimed the response
  contained a `base64` (or `data`) key. PPSSPP contract
  `gpu.record.dump` actually pushes an async event with a `uri`
  field — a `data:application/octet-stream;base64,<payload>` URI.
  See GPURecordSubscriber.cpp:L93. The misleading docstring led
  callers to read `result["base64"]` and silently get KeyError.

Fix: docstring corrected to describe `uri` (data: URI). The
implementation already forwarded the response verbatim, so no
behavior change — only the contract documentation was wrong.

Anchor:
- L4: docstring mentions `uri`, NOT `base64` / `data`.
- L1: WS event name is `gpu.record.dump`; response `uri` field is
  passed through verbatim.
"""

from __future__ import annotations

import inspect

import pytest

from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


class TestV014GpuRecordDumpUri:
    """V014: gpu_record_dump docstring must describe `uri`, not `base64`/`data`."""

    @pytest.mark.asyncio
    async def test_forwards_to_gpu_record_dump_event(self, client, transport):
        """L1 anchor: call forwards to `gpu.record.dump` WS event."""
        transport.set_response(
            "gpu.record.dump",
            {"uri": "data:application/octet-stream;base64,AAECAwQ="},
        )
        await client.gpu_record_dump()
        assert transport.calls[-1][0] == "gpu.record.dump"
        assert transport.calls[-1][1] == {}

    def test_docstring_says_uri_not_base64_or_data(self):
        """L4 anchor: docstring mentions `uri`, not `base64` / `data`.

        The V014 violation shipped a docstring that misled callers into
        reading `result["base64"]` or `result["data"]`. The contract
        returns a data: URI under `uri` — see GPURecordSubscriber.cpp:L93.
        """
        doc = inspect.getdoc(PpssppDebugClient.gpu_record_dump) or ""
        assert "uri" in doc.lower(), (
            "gpu_record_dump docstring MUST mention `uri` (data: URI form). "
            "If this fails, V014 fix was reverted. See "
            "GPURecordSubscriber.cpp:L93."
        )
        # The old docstring claimed the response key was `base64` or
        # `data` — both must be gone from the contract description.
        # We check the Returns section by looking at the full docstring.
        # Note: the word "base64" may legitimately appear as part of
        # "base64-payload" / "base64-encoded" describing the URI's
        # content, so we only forbid the bare key form `base64``.
        assert "`base64`" not in doc, (
            "gpu_record_dump docstring must NOT describe response key "
            "`base64` — the contract returns `uri`. See "
            "GPURecordSubscriber.cpp:L93."
        )
        assert "`data`" not in doc, (
            "gpu_record_dump docstring must NOT describe response key "
            "`data` — the contract returns `uri`. See "
            "GPURecordSubscriber.cpp:L93."
        )

    @pytest.mark.asyncio
    async def test_passthrough_uri_field(self, client, transport):
        """L1 anchor: response `uri` field is passed through verbatim.

        PPSSPP returns `{"uri": "data:application/octet-stream;base64,..."}`.
        debug_client forwards the response dict unchanged. The caller
        reads `result["uri"]` and strips the
        `data:application/octet-stream;base64,` prefix to recover raw
        base64.
        """
        uri_value = "data:application/octet-stream;base64,AAECAwQ="
        transport.set_response("gpu.record.dump", {"uri": uri_value})
        result = await client.gpu_record_dump()
        assert "uri" in result
        assert result["uri"] == uri_value
