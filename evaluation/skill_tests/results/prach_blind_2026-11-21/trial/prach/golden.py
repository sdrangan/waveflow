"""golden.py -- the PRACH searcher's executable definition.

Two models of the same operation, both untimed:

* :func:`search_float` -- the algorithm in float64 (numpy FFTs, no quantization).  The reference the
  fixed-point loss is measured against.
* :func:`search_fixed` -- **bit-exact** with the hardware: stored integers at every step, the two
  FFTs being the library ``SsrFft``'s own bit-exact model (``waveflow.dsp.ssr_fft.model.ssr_fft``),
  every other step written here as integer arithmetic with its rounding and saturation stated.

The steps, per occasion (``n_ant`` antennas, ``n_sym`` repetitions, ``n_roots`` roots):

1. **FFT** each 1024-sample window: ``X[s, a, b]``.
2. **Extract and combine**: PRACH subcarrier ``k`` is bin ``(k_start + k) mod 1024``; sum the
   repetitions coherently, ``A[a, k] = sum_s X[s, a, (k_start + k) % 1024]``; then a **block
   exponent** ``e`` (one per occasion) brings the largest component under 2^15:
   ``Y = sat16(rnd(A / 2^e))``.
3. **Correlate** with each root in frequency: ``Z_r[a, k] = conj(Y[a, k]) * R_u(k)``, with
   ``R_u(k) = conj(x_u(u^-1 k mod 139)) = exp(+j pi u m (m+1) / 139)``, ``m = u^-1 k mod 139`` -- the
   root's DFT up to a constant phase (``y_u(k) = c conj(x_u(u^-1 k))``, ``|c| = sqrt(139)``), generated on
   chip from ``(u, u^-1)`` and a 278-entry cos/sin ROM.  ``Z = sat16(rnd(. / 2^16))``; zero-padded to 256.
4. **FFT 256** of ``Z_r[a]`` (a forward FFT of ``conj(Y) R`` is the conjugate of ``256 x`` the IDFT of
   ``Y conj(R)``: the same magnitude at the same index -- the cyclic cross-correlation).
5. **Power-delay profile**: ``q = sat18(rnd(z / 2^6))``, ``P_r[n] = sum_a |q[a, n]|^2``.
6. **Detect**, per root, per cyclic shift ``v`` with preamble ``p = r * per_root + v < n_pre``: the
   window of FFT-256 indices whose delay ``n * 139/256`` lies in ``[(139 - C_v) mod 139, ... + N_CS)``,
   ``n_start = ceil(((139 - C_v) mod 139) * 256 / 139)``, ``n_end = ceil((((139 - C_v) mod 139) + N_CS) *
   256 / 139)`` (the whole profile when ``N_CS = 0``); the peak and its first index in window order;
   the noise ``S_r = sum P_r[n]`` over the profile **less** the ``2 NOISE_EXCL + 1`` samples round
   every window's peak, ``N_r`` of them; **detected** iff ``peak * N_r * 2^8 > thr_q8 * S_r`` -- the
   peak exceeds ``thr_q8 / 256`` times that mean -- **and** ``peak * REL_Q > max`` (the strongest window
   peak on the root): above -16 dB of it, under which a peak is a stronger UE's sidelobe.

Decision 17 (``decisions.md``): the noise excludes the peaks so that a strong UE does not raise the
threshold of a weak one on its root (near-far), and the relative rule keeps the strong UE's sidelobes
(-13 dB first, 1.43 ZC samples either side) from being reported in the neighbouring windows.

Rounding ``rnd(x / 2^s)`` is round-half-up, ``(x + 2^(s-1)) >> s`` with an arithmetic shift (ap_fixed
``AP_RND``); ``satN`` clamps to the signed N-bit range (``AP_SAT``).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from waveflow.dsp.ssr_fft.model import ssr_fft

from .nr import L_RA, N_FFT, shifts_per_root

N_CORR = 256                     # the correlation FFT
Y_BITS = 16                      # Y, Z: complex int16
Q_SHIFT = 6                      # FFT-256 output (25 bits) -> q (18 bits)
Q_BITS = 18
Z_SHIFT = 16                     # conj(Y) * R (Q15) -> Z
ROM_BITS = 16                    # R: Q15 cos / sin
N_PHASE = 2 * L_RA               # 278 phases of pi / 139
MAX_ANT, MAX_SYM, MAX_PRE, MAX_ROOTS = 4, 12, 64, 64


# ---------------------------------------------------------------------------------------------
# integer helpers
# ---------------------------------------------------------------------------------------------
def rnd_shift(x, s: int):
    """``rnd(x / 2^s)``: round half up (``AP_RND``), arithmetic shift."""
    x = np.asarray(x, dtype=np.int64)
    if s == 0:
        return x
    return (x + (1 << (s - 1))) >> s


def sat(x, bits: int):
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return np.clip(np.asarray(x, dtype=np.int64), lo, hi)


def bit_length_abs(m: int) -> int:
    return int(m).bit_length()


# ---------------------------------------------------------------------------------------------
# the reference ROM and the window table: shared by the golden and the stages
# ---------------------------------------------------------------------------------------------
def phase_rom() -> tuple[np.ndarray, np.ndarray]:
    """``exp(j pi p / 139)``, ``p = 0..277``, as Q15 stored integers: ``rnd(2^15 cos)``, saturated to
    ``[-32768, 32767]`` (``ap_fixed<16, 1>``)."""
    p = np.arange(N_PHASE)
    c = np.clip(np.round(np.cos(np.pi * p / L_RA) * 2 ** 15), -32768, 32767).astype(np.int64)
    s = np.clip(np.round(np.sin(np.pi * p / L_RA) * 2 ** 15), -32768, 32767).astype(np.int64)
    return c, s


def ref_phase_index(u: int, u_inv: int) -> np.ndarray:
    """``p(k) = u m (m + 1) mod 278``, ``m = u_inv k mod 139``, ``k = 0..138``."""
    k = np.arange(L_RA, dtype=np.int64)
    m = (u_inv * k) % L_RA
    return (u * m * (m + 1)) % N_PHASE


#: Every window starts this many profile samples (4 x 130 ns) before its shift's zero-delay point.
#: A UE at zero delay puts its peak at the fractional index ``(139 - C_v) 256 / 139``: half its main
#: lobe falls on the sample below, and its first sidelobe (-13 dB) 2.6 samples below -- both in the
#: neighbouring shift's window without the margin (decision 11 found the main lobe with 2; decision 17
#: the sidelobe, which 2 did not cover: a UE at zero delay lit the next preamble in 30 % of occasions).
#: The cost: the zone's last 520 ns belong to the next shift, and a UE's own late sidelobe needs ~0.5 us
#: more -- round-trip delay plus delay spread must stay ~1 us inside the N_CS zone (`plans/spec.md`).
WIN_MARGIN = 4

#: The noise excludes ``+-NOISE_EXCL`` samples round each window's peak (the main lobe, 1.1 ZC samples).
NOISE_EXCL = 2

#: The relative rule: a peak is reported only if ``peak * REL_Q > `` the root's strongest window peak,
#: i.e. above ``-10 log10(40) = -16.0 dB`` of it (``40 p = (p << 5) + (p << 3)``: no multiplier).
REL_Q = 40


def window(c_v: int, n_cs: int) -> tuple[int, int]:
    """``(n_start, length)`` of cyclic shift ``C_v``'s window on the 256-point profile (mod 256):
    ``n_start = ceil(((139 - C_v) mod 139) 256 / 139) - WIN_MARGIN``, the length
    ``ceil((a + N_CS) 256 / 139) - ceil(a 256 / 139)``; the whole profile when ``N_CS = 0``."""
    if n_cs == 0:
        return 0, N_CORR
    a = (L_RA - c_v) % L_RA
    n_start = (a * N_CORR + L_RA - 1) // L_RA
    n_end = ((a + n_cs) * N_CORR + L_RA - 1) // L_RA
    return (n_start - WIN_MARGIN) % N_CORR, n_end - n_start


def delay_estimate_s(n_peak: int, c_v: int, scs: float = 30e3) -> float:
    """Software's timing estimate from a detection: the peak's delay in ZC samples, ``n 139/256``,
    less the shift's zero-delay position ``(139 - C_v) mod 139``, mod 139, in seconds."""
    d = (n_peak * L_RA / N_CORR - (L_RA - c_v) % L_RA) % L_RA
    if d > L_RA - (WIN_MARGIN + 1) * L_RA / N_CORR:     # in the margin, before the zone: zero delay
        d -= L_RA
    return max(d, 0.0) / (L_RA * scs)


# ---------------------------------------------------------------------------------------------
# the command, and what a search reports
# ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Params:
    n_ant: int
    n_sym: int
    k_start: int
    n_cs: int
    n_pre: int
    roots: tuple[tuple[int, int], ...]          # (u, u_inv) per root, in logical order
    thr_q8: int

    @property
    def per_root(self) -> int:
        return shifts_per_root(self.n_cs)

    @property
    def n_roots(self) -> int:
        return len(self.roots)


@dataclass(frozen=True)
class Detection:
    preamble: int
    n_peak: int
    delay: int            # (n_peak - n_start) mod 256: the offset into its window
    peak: int
    noise: int            # the root's profile sum less the peaks' neighbourhoods
    n_noise: int          # how many samples that sum covers (the mean noise = noise / n_noise)


def roots_for(cfg_roots: list[int]) -> tuple[tuple[int, int], ...]:
    return tuple((u, pow(u, -1, L_RA)) for u in cfg_roots)


# ---------------------------------------------------------------------------------------------
# the fixed-point stages (each a pure function; the hardware stages compute exactly these)
# ---------------------------------------------------------------------------------------------
def fft1024(re: np.ndarray, im: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One window through ``SsrFft(L=1024)``: int16 stored ints in (``ap_fixed<16,2>``), 27-bit out."""
    r, i, _ = ssr_fft(np.asarray(re, dtype=np.int64), np.asarray(im, dtype=np.int64), N_FFT)
    return np.asarray(r, dtype=np.int64), np.asarray(i, dtype=np.int64)


def fft256(re: np.ndarray, im: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    r, i, _ = ssr_fft(np.asarray(re, dtype=np.int64), np.asarray(im, dtype=np.int64), N_CORR)
    return np.asarray(r, dtype=np.int64), np.asarray(i, dtype=np.int64)


def extract_combine(xr: np.ndarray, xi: np.ndarray, k_start: int
                    ) -> tuple[np.ndarray, np.ndarray, int]:
    """FFT outputs ``(n_sym, n_ant, 1024)`` -> ``(Yr, Yi, e)``: ``(n_ant, 139)`` int16 and the block
    exponent.  ``e`` is the smallest ``e >= 0`` with ``max |A| >> e < 2^15``, where ``max |A|`` is the
    largest absolute component over every antenna and subcarrier."""
    bins = (k_start + np.arange(L_RA)) % N_FFT
    ar = np.asarray(xr, dtype=np.int64)[:, :, bins].sum(axis=0)
    ai = np.asarray(xi, dtype=np.int64)[:, :, bins].sum(axis=0)
    m = int(max(np.abs(ar).max(), np.abs(ai).max()))
    e = max(0, bit_length_abs(m) - (Y_BITS - 1))
    return sat(rnd_shift(ar, e), Y_BITS), sat(rnd_shift(ai, e), Y_BITS), e


def correlate(yr: np.ndarray, yi: np.ndarray, u: int, u_inv: int) -> tuple[np.ndarray, np.ndarray]:
    """``(n_ant, 139)`` -> ``(n_ant, 256)``: ``sat16(rnd(conj(Y) R / 2^16))``, zero from 139 up.
    ``re = yr rr + yi ri``, ``im = yr ri - yi rr`` (exact in 34 bits, then rounded once)."""
    c, s = phase_rom()
    p = ref_phase_index(u, u_inv)
    rr, ri = c[p], s[p]
    yr = np.asarray(yr, dtype=np.int64)
    yi = np.asarray(yi, dtype=np.int64)
    zr = sat(rnd_shift(yr * rr + yi * ri, Z_SHIFT), Y_BITS)
    zi = sat(rnd_shift(yr * ri - yi * rr, Z_SHIFT), Y_BITS)
    n_ant = yr.shape[0]
    outr = np.zeros((n_ant, N_CORR), dtype=np.int64)
    outi = np.zeros((n_ant, N_CORR), dtype=np.int64)
    outr[:, :L_RA], outi[:, :L_RA] = zr, zi
    return outr, outi


def power(zr: np.ndarray, zi: np.ndarray) -> np.ndarray:
    """``(n_ant, 256)`` FFT-256 outputs -> the profile ``P[n] = sum_a |sat18(rnd(z / 2^6))|^2``."""
    qr = sat(rnd_shift(zr, Q_SHIFT), Q_BITS)
    qi = sat(rnd_shift(zi, Q_SHIFT), Q_BITS)
    return (qr * qr + qi * qi).sum(axis=0)


def window_peaks(pdp: np.ndarray, r: int, n_cs: int, per_root: int, n_pre: int):
    """Root ``r``'s windows in use: ``[(preamble, n_peak, offset j, peak)]`` (the first maximum in
    window order), and the noise mask -- False on ``+-NOISE_EXCL`` round every peak (mod 256)."""
    pdp = np.asarray(pdp)
    peaks = []
    mask = np.ones(N_CORR, dtype=bool)
    for v in range(per_root):
        pre = r * per_root + v
        if pre >= n_pre:
            break
        n0, ln = window(v * n_cs, n_cs)
        idx = (n0 + np.arange(ln)) % N_CORR
        j = int(np.argmax(pdp[idx]))
        peaks.append((pre, int(idx[j]), j, pdp[idx[j]]))
        mask[(idx[j] + np.arange(-NOISE_EXCL, NOISE_EXCL + 1)) % N_CORR] = False
    return peaks, mask


def detect_root(pdp: np.ndarray, r: int, prm: Params) -> list[Detection]:
    """The detections on root ``r``'s profile, in increasing ``v``: ``peak N 2^8 > thr_q8 S`` (``S``
    the profile less the peaks' neighbourhoods, ``N`` its sample count) and ``peak REL_Q > max``."""
    pdp = np.asarray(pdp, dtype=np.int64)
    peaks, mask = window_peaks(pdp, r, prm.n_cs, prm.per_root, prm.n_pre)
    noise, n_noise = int(pdp[mask].sum()), int(mask.sum())
    pmax = max(int(p) for *_, p in peaks)
    out = []
    for pre, n, j, peak in peaks:
        peak = int(peak)
        if (peak * n_noise << 8) > prm.thr_q8 * noise and peak * REL_Q > pmax:
            out.append(Detection(pre, n, j, peak, noise, n_noise))
    return out


@dataclass
class Trace:
    """Everything between the stages, for the stages' own harnesses and for inspection."""
    yr: np.ndarray
    yi: np.ndarray
    e: int
    pdp: list[np.ndarray]
    dets: list[Detection]


def search_fixed(wr: np.ndarray, wi: np.ndarray, prm: Params) -> Trace:
    """Bit-exact.  ``wr, wi``: ``(n_sym, n_ant, 1024)`` int16 stored integers (the FFT windows)."""
    n_sym, n_ant, _ = wr.shape
    xr = np.empty((n_sym, n_ant, N_FFT), dtype=np.int64)
    xi = np.empty_like(xr)
    for s in range(n_sym):
        for a in range(n_ant):
            xr[s, a], xi[s, a] = fft1024(wr[s, a], wi[s, a])
    yr, yi, e = extract_combine(xr, xi, prm.k_start)
    pdps, dets = [], []
    for r, (u, ui) in enumerate(prm.roots):
        zr, zi = correlate(yr, yi, u, ui)
        fr = np.empty_like(zr)
        fi = np.empty_like(zi)
        for a in range(n_ant):
            fr[a], fi[a] = fft256(zr[a], zi[a])
        pdp = power(fr, fi)
        pdps.append(pdp)
        dets += detect_root(pdp, r, prm)
    return Trace(yr, yi, e, pdps, dets)


# ---------------------------------------------------------------------------------------------
# the float reference
# ---------------------------------------------------------------------------------------------
def search_float(w: np.ndarray, prm: Params, *, return_metric: bool = False):
    """The same algorithm in float64: ``w`` is ``(n_sym, n_ant, 1024)`` complex.  Returns the
    detections ``[(preamble, n_peak, peak / mean)]``; with ``return_metric``, also every window's
    ``peak / mean`` (one per preamble), the statistic the threshold is set on.  ``mean``: the noise
    of :func:`detect_root`, the profile less the peaks' neighbourhoods; the relative rule applies to
    the detections, not to the metric."""
    x = np.fft.fft(w, axis=-1)
    bins = (prm.k_start + np.arange(L_RA)) % N_FFT
    y = x[:, :, bins].sum(axis=0)                                    # (n_ant, 139)
    dets, metric = [], np.zeros(prm.n_pre)
    for r, (u, ui) in enumerate(prm.roots):
        k = np.arange(L_RA)
        m = (ui * k) % L_RA
        ref = np.exp(1j * np.pi * u * m * (m + 1) / L_RA)
        z = np.zeros((y.shape[0], N_CORR), dtype=complex)
        z[:, :L_RA] = np.conj(y) * ref
        pdp = (np.abs(np.fft.fft(z, axis=-1)) ** 2).sum(axis=0)
        peaks, mask = window_peaks(pdp, r, prm.n_cs, prm.per_root, prm.n_pre)
        mean = pdp[mask].mean()
        pmax = max(p for *_, p in peaks)
        for pre, n, _, peak in peaks:
            metric[pre] = peak / mean
            if metric[pre] > prm.thr_q8 / 256 and peak * REL_Q > pmax:
                dets.append((pre, n, float(metric[pre])))
    return (dets, metric) if return_metric else dets
