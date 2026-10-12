"""noise.py -- received power and thermal noise, in the units the RF environment uses.

**Units** (design note, ``plans/sionna_rt/sionna_rt.md`` §14, items 1 and A4): a sample is the
complex envelope in sqrt(W), so ``|x|**2`` is power in watts at the antenna port.  A gain in dB scales
**amplitude** by ``10**(dB/20)``.  dBFS of a complex sample is ``10 log10(|x|**2 / full_scale**2)``,
the scale an :class:`~waveflow.hw.rfdc.Rfdc` quantizes I and Q against.

**Thermal noise** is complex Gaussian with power ``k T B F`` over the sample-rate bandwidth ``B``.
It is drawn by **absolute sample index**, in fixed chunks keyed by ``(stream, channel, chunk)``
(design note, A5), so the noise a run sees does not depend on how it is blocked, two channels never
share a stream, and two nodes with different stream tags never share one either.  The chunk being
read is cached, so drawing one sample at a time costs no more than drawing a block.
"""

from __future__ import annotations

import math
import zlib

import numpy as np

#: Boltzmann's constant, J/K.
BOLTZMANN = 1.380649e-23
#: Reference noise temperature, K.
T0 = 290.0
#: Samples per noise chunk (one draw keys one chunk of one channel).
CHUNK = 4096


def dbm_to_w(dbm: float) -> float:
    """Power in watts from dBm."""
    return 10.0 ** ((float(dbm) - 30.0) / 10.0)


def w_to_dbm(w: float) -> float:
    """Power in dBm from watts."""
    return 10.0 * math.log10(float(w)) + 30.0


def amplitude_gain(db: float) -> float:
    """The amplitude factor of a gain in dB."""
    return 10.0 ** (float(db) / 20.0)


def dbfs(x: np.ndarray, full_scale: float) -> float:
    """Mean power of complex samples relative to ``full_scale**2``, in dBFS."""
    p = float(np.mean(np.abs(np.asarray(x)) ** 2))
    return 10.0 * math.log10(p / float(full_scale) ** 2)


def gain_db_for_dbfs(power_w: float, target_dbfs: float, full_scale: float) -> float:
    """The receive gain that puts a signal of ``power_w`` watts at ``target_dbfs``."""
    return float(target_dbfs) + 10.0 * math.log10(
        float(full_scale) ** 2 / float(power_w)
    )


def thermal_noise_power(
    samp_rate: float, noise_figure_db: float, temperature_k: float = T0
) -> float:
    """``k T B F`` in watts, with ``B`` the sample rate (complex baseband)."""
    return (
        BOLTZMANN
        * float(temperature_k)
        * float(samp_rate)
        * 10.0 ** (noise_figure_db / 10.0)
    )


def stream_tag(name: str) -> int:
    """A stable integer stream tag from a name (a node's name, say)."""
    return zlib.crc32(name.encode("utf-8"))


class ThermalNoise:
    """Complex Gaussian noise of a given power, drawn by absolute sample index.

    ``samples(n_ch, n0, count)`` returns ``(n_ch, count)`` samples for indices ``n0 ..``; the same
    indices give the same samples however the calls are split.
    """

    def __init__(self, power_w: float, seed: int, tag: int) -> None:
        if power_w < 0:
            raise ValueError(f"noise power must be non-negative, got {power_w}")
        self.power_w = float(power_w)
        self.seed = int(seed)
        self.tag = int(tag)
        self._cache: dict[int, tuple[int, np.ndarray]] = {}

    def _chunk(self, ch: int, c: int) -> np.ndarray:
        hit = self._cache.get(ch)
        if hit is not None and hit[0] == c:
            return hit[1]
        ss = np.random.SeedSequence(self.seed, spawn_key=(self.tag, ch, c))
        rng = np.random.Generator(np.random.PCG64(ss))
        z = rng.standard_normal((2, CHUNK))
        data = (z[0] + 1j * z[1]) * math.sqrt(self.power_w / 2.0)
        self._cache[ch] = (c, data)
        return data

    def samples(self, n_ch: int, n0: int, count: int) -> np.ndarray:
        out = np.empty((int(n_ch), int(count)), dtype=np.complex128)
        for ch in range(int(n_ch)):
            pos, n = 0, int(n0)
            while pos < count:
                c, off = divmod(n, CHUNK)
                take = min(CHUNK - off, count - pos)
                out[ch, pos : pos + take] = self._chunk(ch, c)[off : off + take]
                pos += take
                n += take
        return out
