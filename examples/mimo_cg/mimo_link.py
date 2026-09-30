"""mimo_link.py — the floating-point link model for the CG massive-MIMO study.

Step 1.1 of ``plans/mimo_cg/mimo_cg_paper_sims.md``: the uncoded uplink the detectors are
measured on.  ``K`` single-antenna users send Gray-coded square QAM to an ``M``-antenna base
station over an i.i.d. Rayleigh channel:

.. math::

    y = H x + n, \\qquad H_{mk} \\sim \\mathcal{CN}(0, 1), \\qquad
    n \\sim \\mathcal{CN}(0, \\sigma^2 I), \\qquad E|x_k|^2 = E_s = 1.

SNR convention
--------------
``rho`` is the per-user *transmit* SNR, ρ = Es/σ², so σ² = 1/ρ and the MMSE system matrix is
``A = HᴴH + σ²I``.  Without fading (H = I) the symbol SNR is Es/N0 = ρ, and for QPSK the per-bit
SNR is ρ/2.  The receive array adds its gain on top of ρ, which is why BER 1e-3 is reached
between −11 and +11 dB for M = 32…128 (the plan's §9 table) rather than on a 0–30 dB grid.

Statistics
----------
Bit errors inside one independent unit — a symbol under AWGN, a channel realization under
fading — are correlated, so confidence intervals count *units*, not bits.  Each unit's bit-error
fraction lies in [0, 1], so its variance is at most p(1−p), and a Wilson interval over units is
conservative (:func:`wilson_interval`, :class:`BerCount`).

Seeding
-------
Every simulated point draws from its own generator, seeded from a ``SeedSequence`` keyed by the
point's parameters (:func:`point_rng`).  A point's samples therefore do not depend on run order or
on how many workers share a sweep.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist

import numpy as np

#: Base entropy of every point generator.  Changing it re-draws every Monte Carlo sample.
BASE_SEED = 20260930

#: The study's modulations, by name, as square-QAM orders.
MODULATIONS: dict[str, int] = {"qpsk": 4, "16qam": 16, "64qam": 64}

_erfc = np.vectorize(math.erfc, otypes=[float])


def _gray(i: np.ndarray | int) -> np.ndarray | int:
    """Binary-reflected Gray code of ``i``."""
    return i ^ (i >> 1)


@dataclass(frozen=True)
class Qam:
    """Gray-coded square QAM with unit average energy.

    A symbol's ``b = log2(order)`` bits are split in half: the first ``b/2`` (MSB first) pick the
    in-phase level, the rest the quadrature level.  Each half is a Gray label of an ``L``-level PAM
    alphabet ``{-(L-1), …, -1, 1, …, L-1}``, so constellation neighbours differ in exactly one bit.

    Parameters
    ----------
    order : int
        Constellation size: 4 (QPSK), 16 or 64.
    """

    order: int

    def __post_init__(self) -> None:
        b = round(math.log2(self.order)) if self.order > 0 else 0
        if self.order < 4 or 2**b != self.order or b % 2:
            raise ValueError(
                f"square QAM needs an even power of two >= 4, got {self.order}"
            )

    @property
    def bits_per_symbol(self) -> int:
        return round(math.log2(self.order))

    @property
    def levels(self) -> int:
        """PAM levels per dimension, ``L = sqrt(order)``."""
        return round(math.sqrt(self.order))

    @property
    def scale(self) -> float:
        """Amplitude of the unit level: levels ``±1, ±3, …`` have mean energy ``2(order-1)/3``."""
        return 1.0 / math.sqrt(2.0 * (self.order - 1) / 3.0)

    def _label_to_level(self) -> np.ndarray:
        """``table[g]`` = the PAM level index whose Gray label is ``g``."""
        idx = np.arange(self.levels)
        table = np.empty(self.levels, dtype=np.int64)
        table[_gray(idx)] = idx
        return table

    def modulate(self, bits: np.ndarray) -> np.ndarray:
        """Map 0/1 bits, shape ``(..., n * b)``, to symbols, shape ``(..., n)``."""
        b, h, L = self.bits_per_symbol, self.bits_per_symbol // 2, self.levels
        bits = np.asarray(bits, dtype=np.int64)
        bits = bits.reshape(*bits.shape[:-1], -1, b)
        weights = 1 << np.arange(h - 1, -1, -1)
        to_level = self._label_to_level()
        li = to_level[bits[..., :h] @ weights]
        lq = to_level[bits[..., h:] @ weights]
        return self.scale * ((2 * li - (L - 1)) + 1j * (2 * lq - (L - 1)))

    def demodulate(self, z: np.ndarray) -> np.ndarray:
        """Hard-decision (nearest-point) bits, shape ``(..., n * b)``, for estimates ``(..., n)``."""
        h, L = self.bits_per_symbol // 2, self.levels
        z = np.asarray(z)

        def level(x: np.ndarray) -> np.ndarray:
            return np.clip(np.rint((x / self.scale + (L - 1)) / 2), 0, L - 1).astype(
                np.int64
            )

        shifts = np.arange(h - 1, -1, -1)
        bi = (_gray(level(z.real))[..., None] >> shifts) & 1
        bq = (_gray(level(z.imag))[..., None] >> shifts) & 1
        return np.concatenate([bi, bq], axis=-1).reshape(*z.shape[:-1], -1)

    def constellation(self) -> np.ndarray:
        """All ``order`` points; entry ``s`` carries the bits of ``s`` written MSB first."""
        b = self.bits_per_symbol
        labels = np.arange(self.order)
        bits = (labels[:, None] >> np.arange(b - 1, -1, -1)) & 1
        return self.modulate(bits.reshape(-1))


def rayleigh(rng: np.random.Generator, shape: tuple[int, ...]) -> np.ndarray:
    """Array of i.i.d. CN(0, 1) entries."""
    return (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)) * math.sqrt(
        0.5
    )


def noise_variance(rho_db: float) -> float:
    """σ² = 1/ρ for a per-user transmit SNR of ``rho_db`` (unit symbol energy)."""
    return 10.0 ** (-rho_db / 10.0)


def ber_qam_awgn(esn0: np.ndarray | float, order: int) -> np.ndarray:
    """Exact BER of Gray-coded square QAM in AWGN at symbol SNR ``esn0`` (linear).

    The closed form of K. Cho and D. Yoon, "On the general BER expression of one- and
    two-dimensional amplitude modulations," IEEE Trans. Commun. 50(7):1074–1080, 2002.  For
    QPSK it reduces to Q(sqrt(Es/N0)).
    """
    esn0 = np.asarray(esn0, dtype=float)
    sq = round(math.sqrt(order))
    n_bits_dim = round(math.log2(sq))
    arg = np.sqrt(3.0 * esn0 / (2.0 * (order - 1)))
    total = np.zeros_like(esn0)
    for k in range(1, n_bits_dim + 1):
        pk = np.zeros_like(esn0)
        for i in range(int((1 - 2.0**-k) * sq)):
            t = i * 2 ** (k - 1) / sq
            weight = (-1) ** math.floor(t) * (2 ** (k - 1) - math.floor(t + 0.5))
            pk = pk + weight * _erfc((2 * i + 1) * arg)
        total = total + pk / sq
    return total / n_bits_dim


def wilson_interval(p_hat: float, n: int, level: float = 0.999) -> tuple[float, float]:
    """Two-sided Wilson score interval for a proportion ``p_hat`` observed over ``n`` units."""
    if n <= 0:
        return 0.0, 1.0
    z = NormalDist().inv_cdf(0.5 + level / 2.0)
    denom = 1.0 + z * z / n
    center = (p_hat + z * z / (2.0 * n)) / denom
    half = z * math.sqrt(p_hat * (1.0 - p_hat) / n + z * z / (4.0 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


@dataclass
class BerCount:
    """Running bit-error count over independent units (see the module's *Statistics*)."""

    bit_errors: int = 0
    bits: int = 0
    units: int = 0

    def add(self, bit_errors: int, bits: int, units: int) -> None:
        self.bit_errors += int(bit_errors)
        self.bits += int(bits)
        self.units += int(units)

    @property
    def ber(self) -> float:
        return self.bit_errors / self.bits if self.bits else float("nan")

    def interval(self, level: float = 0.999) -> tuple[float, float]:
        """Conservative Wilson interval: bit-error fraction over ``units`` independent units."""
        return wilson_interval(self.ber, self.units, level)


def point_rng(*keys: int) -> np.random.Generator:
    """The generator of one simulated point, seeded from ``(BASE_SEED, *keys)``.

    Keys must be non-negative integers; use :func:`snr_key` for an SNR in dB.
    """
    if any(int(k) != k or k < 0 for k in keys):
        raise ValueError(f"seed keys must be non-negative integers, got {keys}")
    return np.random.default_rng(np.random.SeedSequence([BASE_SEED, *map(int, keys)]))


def snr_key(rho_db: float) -> int:
    """A non-negative seed key for an SNR in dB, at 0.1 dB resolution."""
    return round(rho_db * 10) + 10_000


def awgn_ber(
    qam: Qam, rho_db: float, n_symbols: int, rng: np.random.Generator
) -> BerCount:
    """BER of ``qam`` over AWGN only (H = I), counting each symbol as one independent unit."""
    bits = rng.integers(0, 2, size=n_symbols * qam.bits_per_symbol)
    x = qam.modulate(bits)
    y = x + rayleigh(rng, x.shape) * math.sqrt(noise_variance(rho_db))
    errors = int(np.count_nonzero(qam.demodulate(y) != bits))
    count = BerCount()
    count.add(errors, bits.size, n_symbols)
    return count
