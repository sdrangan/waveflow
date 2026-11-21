"""nr.py -- the 5G NR side of the PRACH searcher: the configuration, the preambles and a UE / channel
model that produces what the gNB's antennas see.  Floating point; no hardware here.

References (3GPP TS 38.211 V17, section 6.3.3):

* short preamble, ``L_RA = 139``, subcarrier spacing ``Df_RA = 30 kHz`` (``mu = 1``), formats A1..C2;
* ``x_u(i) = exp(-j pi u i (i+1) / 139)``, ``i = 0..138`` (6.3.3.1);
* ``x_{u,v}(n) = x_u((n + C_v) mod 139)``, ``C_v = v N_CS`` for the unrestricted set, ``v = 0 ..
  floor(139 / N_CS) - 1`` (``C_v = 0`` only when ``N_CS = 0``);
* the frequency-domain sequence ``y_{u,v}(k) = sum_m x_{u,v}(m) exp(-j 2 pi m k / 139)`` is what sits
  on PRACH subcarrier ``k`` (6.3.3.2's ``a_k``);
* 64 preambles per cell, taken shift-first from consecutive logical roots.

At 30.72 Msps (a 20 MHz carrier at 30 kHz SCS, 51 PRBs, a 1024-point OFDM symbol) a short-preamble
symbol is exactly ``2048 kappa 2^-mu Tc = 1024`` samples, so the PRACH subcarriers fall on 1024-point
FFT bins: PRACH subcarrier ``k`` is bin ``(k_start + k) mod 1024``, where ``k_start`` folds in the
carrier position, ``n_RA_start`` and ``k_bar = 2`` (computed by the host; see :func:`k_start_for`).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

L_RA = 139
N_FFT = 1024                      # 30.72 Msps, 30 kHz
FS = 30.72e6
SCS = 30e3
TC = 1.0 / (480e3 * 4096)         # NR basic time unit

#: Short formats at mu = 1, sampled at 30.72 Msps: (repetitions of the 1024-sample symbol, CP samples).
#: N_u = 2048 kappa 2^-mu Tc per repetition, kappa = 64: 1024 samples; CP = N_CP kappa 2^-mu Tc.
FORMATS = {
    "A1": (2, 144), "A2": (4, 288), "A3": (6, 432),
    "B1": (2, 216), "B2": (4, 360), "B3": (6, 504), "B4": (12, 468),
    "C0": (1, 620), "C2": (4, 1024),
}

#: zeroCorrelationZoneConfig -> N_CS for L_RA = 139 (TS 38.211 Table 6.3.3.1-7).
NCS_TABLE_139 = [0, 2, 4, 6, 8, 10, 12, 13, 15, 17, 19, 23, 27, 34, 46, 69]


def logical_to_physical_root(i: int) -> int:
    """TS 38.211 Table 6.3.3.1-4 for L_RA = 139: logical root ``i`` -> ``u``.  The table runs
    1, 138, 2, 137, 3, 136, ... (pairs ``(n, 139 - n)``); **assumed** from that pattern -- check it
    against the table before a deployment (the hardware takes physical ``u`` and does not depend on it)."""
    i = i % (L_RA - 1)
    n = i // 2 + 1
    return n if i % 2 == 0 else L_RA - n


def zc(u: int) -> np.ndarray:
    i = np.arange(L_RA)
    return np.exp(-1j * np.pi * u * i * (i + 1) / L_RA)


def shifts_per_root(n_cs: int) -> int:
    return 1 if n_cs == 0 else L_RA // n_cs


@dataclass(frozen=True)
class CellConfig:
    """What the host (software) knows about the cell's PRACH: everything the command carries."""
    n_cs: int = 23                    # zeroCorrelationZoneConfig 11
    root0: int = 0                    # prach-RootSequenceIndex (logical)
    n_pre: int = 64                   # preambles in the cell
    fmt: str = "B4"
    k_start: int = 2                  # FFT bin of PRACH subcarrier 0 (k1 + k_bar, mod 1024)

    @property
    def per_root(self) -> int:
        return shifts_per_root(self.n_cs)

    @property
    def n_roots(self) -> int:
        return -(-self.n_pre // self.per_root)

    @property
    def roots(self) -> list[int]:
        return [logical_to_physical_root(self.root0 + r) for r in range(self.n_roots)]

    @property
    def n_sym(self) -> int:
        return FORMATS[self.fmt][0]

    @property
    def n_cp(self) -> int:
        return FORMATS[self.fmt][1]

    def preamble(self, p: int) -> tuple[int, int]:
        """Preamble index -> (physical root u, cyclic shift C_v)."""
        r, v = divmod(p, self.per_root)
        return self.roots[r], v * self.n_cs

    def max_delay_s(self) -> float:
        """The largest round-trip delay a zone covers: N_CS ZC samples."""
        return self.n_cs / (L_RA * SCS)


def k_start_for(n_rb_grid: int = 51, rb_start: int = 0, k_bar: int = 2) -> int:
    """The FFT bin of PRACH subcarrier 0 for a PRACH whose first PRB is ``rb_start`` of an
    ``n_rb_grid``-PRB carrier centred on DC: ``k1 = 12 rb_start - 6 n_rb_grid`` (38.211 6.3.3.2
    with ``k0 = 0`` and the BWP at the carrier start), plus ``k_bar``; negative bins wrap."""
    return (12 * rb_start - 6 * n_rb_grid + k_bar) % N_FFT


def freq_sequence(u: int, c_v: int) -> np.ndarray:
    """``y_{u,v}(k)``: what PRACH subcarrier ``k`` carries."""
    return np.fft.fft(np.roll(zc(u), -c_v))


@dataclass(frozen=True)
class Ue:
    """One transmitting UE: its preamble, its round-trip delay (seconds), its SNR per antenna (dB,
    over the PRACH bandwidth: signal power on the 139 subcarriers against the noise on them), a
    residual frequency offset (Hz) and a per-antenna channel."""
    preamble: int
    delay_s: float = 0.0
    snr_db: float = 10.0
    cfo_hz: float = 0.0
    channel: str = "flat"             # "flat": one random phase per antenna; "awgn": unit gain;
                                      # "tdl": Rayleigh taps TDL_DELAYS / TDL_POWERS per antenna


#: A multipath profile for robustness runs: five Rayleigh taps over 1 us, exponential, ~0.3 us rms
#: delay spread.  **Not** a 38.141 TDL model -- an assumption standing in for one.
TDL_DELAYS = np.array([0.0, 0.1, 0.3, 0.6, 1.0]) * 1e-6
TDL_POWERS = np.exp(-TDL_DELAYS / 0.3e-6)
TDL_POWERS = TDL_POWERS / TDL_POWERS.sum()


def occasion(cfg: CellConfig, ues: list[Ue], n_ant: int, rng: np.random.Generator, *,
             noise: bool = True, pusch_db: float | None = None, scale: float = 1.0) -> np.ndarray:
    """The received occasion, ``(n_ant, n_cp + n_sym * 1024)`` complex, unit noise power per
    subcarrier (so per time sample, N_FFT normalisation: ``E|n|^2 = 1/1024`` per sample ... scaled).

    Built in the time domain the way a UE transmits (38.211: CP, then ``n_sym`` repetitions), delayed
    by a fractional delay applied in frequency, through a per-antenna channel, plus noise and, if
    ``pusch_db`` is given, a wideband PUSCH-like interferer on every other subcarrier of the 51 PRBs
    (it must not leak into the PRACH bins).
    """
    n_cp, n_sym = cfg.n_cp, cfg.n_sym
    n_tot = n_cp + n_sym * N_FFT
    t = np.arange(n_tot)
    out = np.zeros((n_ant, n_tot), dtype=complex)
    bins = (cfg.k_start + np.arange(L_RA)) % N_FFT
    # signed subcarrier frequency index of each bin (for the delay ramp)
    fidx = np.where(bins >= N_FFT // 2, bins - N_FFT, bins)
    for ue in ues:
        u, c_v = cfg.preamble(ue.preamble)
        y = freq_sequence(u, c_v) / np.sqrt(L_RA)          # unit power per subcarrier
        amp = np.sqrt(10 ** (ue.snr_db / 10))
        n_late = int(np.floor(ue.delay_s * FS))
        for a in range(n_ant):
            # the channel on each subcarrier: a delay tau multiplies subcarrier f by
            # exp(-j 2 pi f SCS tau); "tdl" adds block-fading Rayleigh taps, independent per antenna
            if ue.channel == "tdl":
                g = (rng.normal(size=len(TDL_DELAYS)) + 1j * rng.normal(size=len(TDL_DELAYS)))
                g *= np.sqrt(TDL_POWERS / 2)
                hf = (g[None, :] * np.exp(-2j * np.pi * fidx[:, None] * SCS * TDL_DELAYS[None, :])).sum(1)
            elif ue.channel == "awgn":
                hf = np.ones(L_RA)
            else:
                hf = np.full(L_RA, np.exp(2j * np.pi * rng.uniform()))
            grid = np.zeros(N_FFT, dtype=complex)
            grid[bins] = y * hf * np.exp(-2j * np.pi * fidx * SCS * ue.delay_s)
            sym = np.fft.ifft(grid) * np.sqrt(N_FFT)         # unit power per subcarrier after FFT/sqrt(N)
            # periodic in the symbol: CP + repetitions; a delay <= CP is circular within each window.
            sig = sym[(t - n_cp) % N_FFT]
            sig[:n_late] = 0                                  # nothing before the first arrival
            sig = sig * np.exp(2j * np.pi * ue.cfo_hz * t / FS)
            out[a] += amp * sig
    if pusch_db is not None:
        data_bins = np.array([b for b in range(-306, 306) if b % 2 == 0]) % N_FFT
        data_bins = np.setdiff1d(data_bins, bins)
        amp = np.sqrt(10 ** (pusch_db / 10))
        for a in range(n_ant):
            for s in range(-1, n_sym):              # symbol-aligned with the windows
                g = np.zeros(N_FFT, dtype=complex)
                g[data_bins] = amp * np.exp(2j * np.pi * rng.uniform(size=len(data_bins)))
                ts = np.fft.ifft(g) * np.sqrt(N_FFT)
                lo = n_cp + s * N_FFT
                seg = np.arange(max(lo, 0), min(lo + N_FFT, n_tot))
                out[a, seg] += ts[seg - lo]
    if noise:
        out += (rng.normal(size=out.shape) + 1j * rng.normal(size=out.shape)) / np.sqrt(2)
    return out * scale


def windows(rx: np.ndarray, cfg: CellConfig) -> np.ndarray:
    """The FFT windows the capture front-end cuts: CP dropped, ``(n_sym, n_ant, 1024)``."""
    n_ant = rx.shape[0]
    w = np.empty((cfg.n_sym, n_ant, N_FFT), dtype=rx.dtype)
    for s in range(cfg.n_sym):
        lo = cfg.n_cp + s * N_FFT
        w[s] = rx[:, lo:lo + N_FFT]
    return w


def quantize_adc(w: np.ndarray, backoff_db: float = 12.0, rng=None) -> tuple[np.ndarray, np.ndarray]:
    """An AGC'd 16-bit ADC: scale so the RMS of a component sits ``backoff_db`` below full scale,
    round, saturate.  Returns int16 ``(re, im)``."""
    rms = np.sqrt(np.mean(np.abs(w) ** 2) / 2)
    g = (2 ** 15) * 10 ** (-backoff_db / 20) / max(rms, 1e-30)
    re = np.clip(np.round(w.real * g), -32768, 32767).astype(np.int64)
    im = np.clip(np.round(w.imag * g), -32768, 32767).astype(np.int64)
    return re, im
