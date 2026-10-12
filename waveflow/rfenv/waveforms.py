"""waveforms.py -- what a transmitter sends: periodic complex-baseband waveforms, indexed by sample.

A source asks for samples by **absolute index** (``samples(n0, count, samp_rate)``), never by
"the next block", so the waveform it plays does not depend on how the run is blocked -- the same
discipline as :class:`~waveflow.rfenv.apply.ChannelApplier` and the noise.

**Unit average power**, for the built-in waveforms, so a source scales by sqrt(P_tx) and gets
``P_tx`` watts (design note, ``plans/sionna_rt/sionna_rt.md`` §14 item 1).  A user's array is played
as given: its power is the user's statement.

A waveform returns ``(count,)`` samples, which a source sends on every transmit channel, or
``(n_ch, count)`` from a multi-channel :class:`ArrayWaveform`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class Waveform:
    """Base class: a waveform defined for every absolute sample index ``n >= 0``."""

    #: Transmit channels the waveform carries, or ``None`` for one sequence sent on all of them.
    n_ch: int | None = None

    def samples(self, n0: int, count: int, samp_rate: float) -> np.ndarray:
        """Samples ``n0 .. n0 + count - 1`` at ``samp_rate``, complex128."""
        raise NotImplementedError


class PeriodicWaveform(Waveform):
    """A waveform that repeats one period of :meth:`period` samples.

    The period is built once and kept (read-only): a PN register is a Python loop, and rebuilding it
    for every block cost 25 ms per block at degree 16 (M2 review).
    """

    def _make_period(self) -> np.ndarray:
        raise NotImplementedError

    def period(self) -> np.ndarray:
        cached = self.__dict__.get(
            "_period"
        )  # set past the frozen dataclass's __setattr__
        if cached is None:
            cached = np.asarray(self._make_period(), dtype=np.complex128)
            cached.setflags(write=False)
            self.__dict__["_period"] = cached
        return cached

    def samples(self, n0: int, count: int, samp_rate: float) -> np.ndarray:
        seq = self.period()
        idx = (int(n0) + np.arange(int(count))) % seq.shape[-1]
        return seq[..., idx]


@dataclass(frozen=True)
class Tone(Waveform):
    """A complex exponential at ``freq_hz`` (baseband offset from the carrier), unit power."""

    freq_hz: float = 0.0
    phase_rad: float = 0.0

    def samples(self, n0: int, count: int, samp_rate: float) -> np.ndarray:
        n = int(n0) + np.arange(int(count), dtype=np.float64)
        return np.exp(
            1j * (2 * np.pi * self.freq_hz * n / float(samp_rate) + self.phase_rad)
        )


@dataclass(frozen=True)
class ZadoffChu(PeriodicWaveform):
    """A Zadoff-Chu sequence of ``length`` with ``root``, repeated: constant amplitude, ideal
    periodic autocorrelation (zero at every nonzero lag), the classic sounding sequence.

    ``x[n] = exp(-j pi root n (n + length mod 2) / length)``; ``root`` must be coprime with
    ``length``.
    """

    length: int = 1021
    root: int = 1

    def __post_init__(self) -> None:
        if self.length < 2 or math.gcd(int(self.root), int(self.length)) != 1:
            raise ValueError(
                f"ZadoffChu needs length >= 2 and root coprime with it; got length "
                f"{self.length}, root {self.root}"
            )

    def _make_period(self) -> np.ndarray:
        n = np.arange(self.length, dtype=np.float64)
        return np.exp(-1j * np.pi * self.root * n * (n + self.length % 2) / self.length)


#: Feedback taps (1-based register positions) of a maximal-length LFSR per degree.
_MSEQ_TAPS = {
    3: (3, 2),
    4: (4, 3),
    5: (5, 3),
    6: (6, 5),
    7: (7, 6),
    8: (8, 6, 5, 4),
    9: (9, 5),
    10: (10, 7),
    11: (11, 9),
    12: (12, 6, 4, 1),
    13: (13, 4, 3, 1),
    14: (14, 5, 3, 1),
    15: (15, 14),
    16: (16, 15, 13, 4),
}


@dataclass(frozen=True)
class Pn(PeriodicWaveform):
    """A maximal-length PN sequence (m-sequence) of degree ``degree``, BPSK +/-1, repeated.

    Period ``2**degree - 1``; periodic autocorrelation ``N`` at lag 0 and ``-1`` elsewhere.
    """

    degree: int = 10

    def __post_init__(self) -> None:
        if self.degree not in _MSEQ_TAPS:
            raise ValueError(
                f"Pn degree must be one of {sorted(_MSEQ_TAPS)}, got {self.degree}"
            )

    def _make_period(self) -> np.ndarray:
        taps = _MSEQ_TAPS[self.degree]
        state = [1] * self.degree
        bits = np.empty(2**self.degree - 1, dtype=np.float64)
        for i in range(bits.size):
            bits[i] = state[-1]
            fb = 0
            for t in taps:
                fb ^= state[t - 1]
            state = [fb, *state[:-1]]
        return (1.0 - 2.0 * bits).astype(np.complex128)


@dataclass(frozen=True, eq=False)
class ArrayWaveform(PeriodicWaveform):
    """A user's samples, ``(L,)`` or ``(n_ch, L)``, repeated with period ``L``; played as given.

    ``data`` has no default (an all-zero waveform would hide a forgotten argument), and two
    waveforms are equal only if they are the same object (``==`` on arrays is elementwise).
    """

    data: np.ndarray

    def __post_init__(self) -> None:
        d = np.array(self.data, dtype=np.complex128)
        if d.ndim not in (1, 2) or d.shape[-1] < 1:
            raise ValueError(
                f"ArrayWaveform data must be (L,) or (n_ch, L); got {d.shape}"
            )
        d.setflags(write=False)
        object.__setattr__(self, "data", d)

    @property
    def n_ch(self) -> int | None:  # type: ignore[override]
        return None if self.data.ndim == 1 else int(self.data.shape[0])

    def _make_period(self) -> np.ndarray:
        return self.data

    @classmethod
    def from_bundle(
        cls, bundle_dir: str | Path, n_ch: int, blksize: int
    ) -> ArrayWaveform:
        """Play a complex RF bundle (:func:`~waveflow.simulation.rf_tb.write_rf_bundle`) as a waveform."""
        from waveflow.simulation.rf_tb import read_rf_bundle

        blocks = read_rf_bundle(bundle_dir, int(n_ch), int(blksize), complex_samp=True)
        data = np.concatenate(blocks, axis=1)
        return cls(data=data[0] if n_ch == 1 else data)
