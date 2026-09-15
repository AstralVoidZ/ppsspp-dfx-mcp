"""Replay domain models (frozen dataclass).

Backs the `ppsspp_replay` aggregate tool. 10 actions cover the full
PPSSPP Replay protocol (see the upstream
Debugger/WebSocket/ReplaySubscriber.cpp):
begin / abort / flush / execute / status / time_get / time_set / save
/ load / wait_complete.

P0 (this file): thin proxy over the 6 WS events + 1 client-side poller
(wait_complete).  P1 will extend `save` / `load` with .ppr file I/O.

Spike evidence:
- U1: `input.buttons.press` recorded accurately during `replay.begin`
- U2: `gpu.buffer.screenshot` unusable during recording (requires
  stepping) — `state_probe` (read_u32) used instead (see
  experiment_state_probe_running_v1.md).
- U3: same-session `execute` works; cross-session deferred (needs save
  state).  `wait_complete` added because `execute` does not auto-end.
"""

from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass
from typing import Any

# ── Replay blob parsing (R3, analysis_replay_u7_and_rtc_root_cause_v1) ──
#
# `replay.flush` returns a HEADERLESS event table (ReplayFlushBlob —
# "No header is flushed with this operation"): a packed sequence of
# ReplayItemHeader records (Core/Replay.cpp, #pragma pack(1)):
#
#     ReplayAction action;   // u8, bit 0x80 = MASK_SIDEDATA
#     u64_le    timestamp;   // CoreTiming::GetGlobalTimeUs() at record time
#     union { u32_le buttons | u8 analog[2][2] | u32_le result
#             | u64_le result64 | u32_le size; }  // 8 bytes
#
# = 17 bytes. MASK_SIDEDATA items carry `u.size` extra payload bytes
# (e.g. FILE_READ data) after the header — the blob length is therefore
# NOT a multiple of 17 in general (verified against a real game macro
# recording: 30 items, 5 with sidedata, exact consumption).
#
# Timestamps are ABSOLUTE per recording session's boot (the U7 root
# cause): first item = recording-time offset from boot, last item = when
# the timeline finishes. That is exactly what the boot-aligned replay
# sequence needs to predict injection start and completion.

_REPLAY_ITEM_HEADER_SIZE = 17
_REPLAY_MASK_SIDEDATA = 0x80


@dataclass(frozen=True)
class ReplayBlobSpan:
    """Parsed timeline facts of a replay blob.

    Attributes:
        event_count: Total items walked (including sidedata ones).
        t0_s: First event timestamp in seconds — game-clock offset from
            the RECORDING session's boot where the timeline begins.
        end_s: Last event timestamp in seconds — game-clock offset where
            the timeline finishes (all events injected).
    """

    event_count: int
    t0_s: float
    end_s: float


def parse_replay_blob_b64(base64_data: str) -> ReplayBlobSpan:
    """Walk a base64 replay blob and return its timeline span.

    Raises:
        ToolError-free ValueError: empty/undecodable base64, or a blob
            that does not walk exactly to its end (truncated header /
            sidedata length corruption).
    """
    if not base64_data:
        raise ValueError("empty replay blob")
    try:
        blob = base64.b64decode(base64_data, validate=True)
    except (ValueError, binascii.Error) as e:
        raise ValueError(f"invalid base64 replay blob: {e}") from e
    count = 0
    first = last = None
    off = 0
    while off < len(blob):
        if off + _REPLAY_ITEM_HEADER_SIZE > len(blob):
            raise ValueError(
                f"truncated replay blob: {len(blob) - off} trailing bytes "
                f"at offset {off} are smaller than a "
                f"{_REPLAY_ITEM_HEADER_SIZE}-byte item header"
            )
        action = blob[off]
        (ts,) = struct.unpack_from("<Q", blob, off + 1)
        (u32,) = struct.unpack_from("<I", blob, off + 9)
        off += _REPLAY_ITEM_HEADER_SIZE
        if action & _REPLAY_MASK_SIDEDATA:
            off += u32
        count += 1
        if first is None:
            first = ts
        last = ts
    if off != len(blob):
        raise ValueError(
            f"corrupt replay blob: sidedata walk ended at {off} != "
            f"{len(blob)} (bad payload length)"
        )
    return ReplayBlobSpan(
        event_count=count, t0_s=first / 1e6, end_s=last / 1e6
    )


@dataclass(frozen=True)
class ReplayResult:
    """Result of a replay action (begin / abort / flush / execute / status
    / time_get / time_set / save / load / wait_complete).

    Attributes:
        action: One of 'begin' / 'abort' / 'flush' / 'execute' / 'status'
            / 'time_get' / 'time_set' / 'save' / 'load' / 'wait_complete'.
        executing: True if a replay is currently executing (from
            `replay.status`).  Drives `wait_complete`'s exit condition.
        saving: True if a replay recording is in progress (from
            `replay.status`).  After `begin`, saving=True; after `flush`
            or `abort`, saving=False.
        version: Recording format version reported by `replay.flush`
            (currently 1).  0 when the action does not return a version.
        size: Recording size in bytes reported by `replay.flush`.  0 when
            the action does not return a size.
        base64: Base64-encoded recording payload from `replay.flush`, or
            the input payload passed to `replay.execute`.  Empty string
            when the action does not carry a payload.
        base_rtc: Base RTC timestamp (seconds) from `replay.time.get`
            / `replay.time.set`.  0 when the action does not return it.
        data: Raw PPSSPP response dict (echoed for diagnostic / future
            field extraction).  Empty dict when the action returns no
            additional fields.
        wait_iterations: Number of `replay.status` polls performed by
            `wait_complete` before exiting.  0 for non-wait actions.
    """

    action: str = "status"
    executing: bool = False
    saving: bool = False
    version: int = 0
    size: int = 0
    base64: str = ""
    base_rtc: int = 0
    data: dict[str, Any] | None = None
    wait_iterations: int = 0


# PPR file format version (independent of replay protocol version).
# Bumped only when the .ppr JSON schema changes incompatibly.
_PPR_FORMAT_VERSION = 1


@dataclass(frozen=True)
class PPRFile:
    """.ppr file format — JSON wrapper around a recorded replay.

    Schema (v1):
    ```json
    {
      "ppr_format_version": 1,
      "version": 1,                // replay protocol version (from flush)
      "base64": "...",              // base64-encoded recording payload
      "base_rtc": 1785051771,       // base RTC at record time (seconds)
      "recorded_at": 1785051799.18, // wall-clock timestamp (time.time())
      "session_note": ""            // optional human-readable note
    }
    ```

    The .ppr file is the persistence layer for P1 (file I/O). The
    recording payload (version + base64) comes from `replay.flush`;
    base_rtc comes from `replay.time_get`. Cross-session playback
    requires restoring base_rtc via `replay.time.set` before
    `replay.execute`.
    """

    version: int = 0
    base64: str = ""
    base_rtc: int = 0
    recorded_at: float = 0.0
    session_note: str = ""
    ppr_format_version: int = _PPR_FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict (for json.dump)."""
        return {
            "ppr_format_version": self.ppr_format_version,
            "version": self.version,
            "base64": self.base64,
            "base_rtc": self.base_rtc,
            "recorded_at": self.recorded_at,
            "session_note": self.session_note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PPRFile:
        """Deserialize from a parsed JSON dict.

        Validates `ppr_format_version` and raises ValueError on mismatch
        or missing required fields.
        """
        raw_fmt = data.get("ppr_format_version")
        if raw_fmt is None:
            raise ValueError(
                "invalid .ppr file: missing 'ppr_format_version' field"
            )
        if int(raw_fmt) != _PPR_FORMAT_VERSION:
            raise ValueError(
                f"unsupported .ppr format version: got {raw_fmt}, "
                f"expected {_PPR_FORMAT_VERSION}"
            )
        for field in ("version", "base64", "base_rtc"):
            if field not in data:
                raise ValueError(
                    f"invalid .ppr file: missing required field {field!r}"
                )
        return cls(
            version=int(data["version"]),
            base64=str(data["base64"]),
            base_rtc=int(data["base_rtc"]),
            recorded_at=float(data.get("recorded_at", 0.0)),
            session_note=str(data.get("session_note", "")),
            ppr_format_version=_PPR_FORMAT_VERSION,
        )
