"""Input view — public JSON contract for input tools."""

from __future__ import annotations

from pydantic import Field

from ppsspp_dfx_mcp.models.input import (
    ButtonPressResult,
    HoldButtonsResult,
    SendAnalogResult,
    WaitFramesResult,
)
from ppsspp_dfx_mcp.views._base import FrozenModel


class ButtonPressResponse(FrozenModel):
    """Response view for ppsspp_press_button."""

    button: str = Field(description="Button name pressed.")
    duration: int = Field(description="Press duration in frames.")

    @classmethod
    def from_result(cls, result: ButtonPressResult) -> ButtonPressResponse:
        return cls(button=result.button, duration=result.duration)


class HoldButtonsResponse(FrozenModel):
    """Response view for ppsspp_hold_buttons."""

    buttons: str = Field(
        description="Button combination string (e.g. 'cross|circle').",
    )

    @classmethod
    def from_result(cls, result: HoldButtonsResult) -> HoldButtonsResponse:
        return cls(buttons=result.buttons)


class SendAnalogResponse(FrozenModel):
    """Response view for ppsspp_send_analog."""

    x: int = Field(description="X coordinate in [0, 255] (128 = center).")
    y: int = Field(description="Y coordinate in [0, 255] (128 = center).")

    @classmethod
    def from_result(cls, result: SendAnalogResult) -> SendAnalogResponse:
        return cls(x=result.x, y=result.y)


class WaitFramesResponse(FrozenModel):
    """Response view for ppsspp_wait_frames."""

    frames: int = Field(description="Number of frames waited.")
    elapsed_s: float = Field(description="Wall-clock seconds elapsed.")

    @classmethod
    def from_result(cls, result: WaitFramesResult) -> WaitFramesResponse:
        return cls(frames=result.frames, elapsed_s=result.elapsed_s)
