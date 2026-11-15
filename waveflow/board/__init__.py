"""Physical FPGA board targets and explicit Vivado clock resolution."""

from .spec import Board, ClockProfile, ClockSource
from .clock import ClockProbeError, ClockRequest, ClockRequestError, ClockResolution

__all__ = [
    "Board", "ClockProfile", "ClockSource", "ClockRequest", "ClockResolution",
    "ClockProbeError", "ClockRequestError",
]
