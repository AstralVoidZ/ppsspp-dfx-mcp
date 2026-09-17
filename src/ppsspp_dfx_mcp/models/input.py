"""Input domain models (frozen dataclass)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Button names accepted by PPSSPP's WebSocket debugger. Source of truth:
# InputSubscriber.cpp buttonLookup table (25 entries) — the PSP
# face/shoulder/system buttons plus the console-only keys (home, screen,
# note, hold, wlan, remote, volume, disc, memstick, playback).
PPSSPP_ALL_BUTTONS: tuple[str, ...] = (
    "cross",
    "circle",
    "triangle",
    "square",
    "up",
    "down",
    "left",
    "right",
    "start",
    "select",
    "home",
    "screen",
    "note",
    "ltrigger",
    "rtrigger",
    "hold",
    "wlan",
    "remote_hold",
    "vol_up",
    "vol_down",
    "disc",
    "memstick",
    "forward",
    "back",
    "playpause",
)

PSPButton = Literal[*PPSSPP_ALL_BUTTONS]


@dataclass(frozen=True)
class ButtonPressResult:
    """Result of a button press.

    Attributes:
        button: Button name pressed.
        duration: Press duration in frames.
    """

    button: str = ""
    duration: int = 10


@dataclass(frozen=True)
class HoldButtonsResult:
    """Result of a hold-buttons operation.

    Attributes:
        buttons: Button combination string (e.g. 'cross|circle').
    """

    buttons: str = ""


@dataclass(frozen=True)
class SendAnalogResult:
    """Result of an analog stick send.

    Attributes:
        x: X coordinate in [0, 255] (128 = center).
        y: Y coordinate in [0, 255] (128 = center).
    """

    x: int = 128
    y: int = 128


@dataclass(frozen=True)
class WaitFramesResult:
    """Result of a frame-wait operation.

    Attributes:
        frames: Number of frames waited.
        elapsed_s: Wall-clock seconds elapsed.
    """

    frames: int = 0
    elapsed_s: float = 0.0
