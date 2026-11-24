"""Physical FPGA board targets and explicit Vivado clock resolution."""

from .spec import Board, ReferenceClock, ClockDevice, ClockProfile, ClockSource
from .clock import ClockProbeError, ClockRequest, ClockRequestError, ClockResolution

__all__ = [
    "Board", "ReferenceClock", "ClockDevice", "ClockProfile", "ClockSource", "ClockRequest", "ClockResolution",
    "ClockProbeError", "ClockRequestError",
]
