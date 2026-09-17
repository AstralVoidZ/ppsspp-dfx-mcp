"""GPU record dump domain model (frozen dataclass).

Wraps the result of `PpssppDebugClient.gpu_record_dump()` — a READ-ONLY
operation that calls `gpu.record.dump` (async ticketed: PPSSPP records
the next frame's GE commands and returns them as a binary dump).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any


def _extract_bytes(raw: dict[str, Any]) -> bytes:
    """Extract binary dump from a `gpu.record.dump` response.

    PPSSPP returns the dump as a data URI in the `uri` field
    (e.g., 'data:application/octet-stream;base64,...'). Also tolerates
    raw base64 in `base64` or `data` fields. Returns b"" if none present.
    """
    uri = raw.get("uri", "")
    if uri and uri.startswith("data:"):
        # Extract base64 payload from data URI
        # Format: data:<mime>;base64,<payload>
        b64_start = uri.find("base64,")
        b64 = uri[b64_start + 7 :] if b64_start >= 0 else ""
    else:
        b64 = raw.get("base64", "") or raw.get("data", "")
    if not b64:
        return b""
    try:
        return base64.b64decode(b64)
    except (TypeError, ValueError, base64.binascii.Error):
        return b""


@dataclass(frozen=True)
class GpuRecordResult:
    """Result of a `gpu.record.dump` query.

    Attributes:
        data: Decoded binary GE command dump. Empty bytes if PPSSPP
            returned no data.
        size: Length of `data` in bytes.
        raw: Full raw response from `gpu.record.dump`. Preserved so
            clients can access metadata fields not explicitly modeled.
    """

    data: bytes = b""
    size: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> GpuRecordResult:
        """Build GpuRecordResult from a raw `gpu.record.dump` response."""
        data = _extract_bytes(raw)
        return cls(
            data=data,
            size=len(data),
            raw=dict(raw),
        )
