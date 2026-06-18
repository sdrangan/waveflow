from __future__ import annotations

from waveflow.named import NamedObject

from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class Clock(NamedObject):
    """ Represents a clock domain in the hardware design."""
    freq: float = 1e9
    """ The frequency of the clock in Hz. """

    @property
    def period(self) -> float:
        return 1.0 / self.freq
