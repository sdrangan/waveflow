"""golden.py -- the bit-exact golden model of the detector (``docs/examples/aoa_detect/algo.md``).

:class:`AoaGolden` runs every channel's search / track state machine on blocks of integer samples, with
**no loss**: every block is examined and every decision is made at once, as if the hardware were
infinitely fast.  It is the reference for the accuracy metrics (false alarms, missed detections, early
stops, angular error) and for the number formats; the timed models add the delays, drops and late reads.

**The arithmetic** (``param.md``, *Number formats*).  Sums are exact (int64).  The values kept between
stages are stored narrower, each as ``bits`` signed bits after a right shift with rounding, and an
overflow ``mode`` (``"saturate"`` or ``"wrap"``):

=========  ===================================  ======================================================
value      exact                                stored
=========  ===================================  ======================================================
Q          ``X Xᴴ`` over the block              ``q_bits``, shifted by ``q_shift``
U          --                                   steering vectors at ``u_bits`` (amplitude ``2^(u_bits-1)-1``)
p          ``Re(uᴴ (Q U))`` per direction       ``p_bits``, shifted by ``p_shift``
y          ``uᴴ x`` per sample (departure)      ``y_bits``, shifted by ``y_shift``
pb         ``Σ |y|²`` over the block            ``pb_bits``, shifted by ``pb_shift``
=========  ===================================  ======================================================

Each shift is chosen from the noise level so that the noise-only value sits ``*_headroom`` bits below
full scale: a strong emitter saturates a value once it is that many bits above the noise.  The
headrooms (and widths and modes) are the programmable parameters; the shifts follow.
``fixed=False`` runs the same pipeline in float64 (no rounding, no overflow) for comparison.

**Thresholds** are set from targets (``pfa_gate``, ``pfa_detect``, ``gamma_stop``) with the chi-square
statistics of ``algo.md`` and the calibrated noise power ``N0 = noise_lsb² + 1/6`` LSB² (thermal plus
rounding), and stored as numbers in the units each comparison sees, as the host would program them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from examples.aoa_detect.array import steering

# --- the chi-square tail, without scipy ------------------------------------------------------------


def _gammaq(a: float, x: float) -> float:
    """The regularized upper incomplete gamma function ``Q(a, x)`` (series or continued fraction)."""
    if x <= 0.0:
        return 1.0
    lg = math.lgamma(a)
    if x < a + 1.0:                                   # series for P, then Q = 1 - P
        ap, s, d = a, 1.0 / a, 1.0 / a
        for _ in range(100_000):
            ap += 1.0
            d *= x / ap
            s += d
            if abs(d) < abs(s) * 1e-15:
                break
        return 1.0 - s * math.exp(-x + a * math.log(x) - lg)
    b = x + 1.0 - a                                   # Lentz's continued fraction for Q
    c, d = 1.0 / 1e-300, 1.0 / b
    h = d
    for i in range(1, 100_000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        d = 1e-300 if abs(d) < 1e-300 else d
        c = b + an / c
        c = 1e-300 if abs(c) < 1e-300 else c
        d = 1.0 / d
        de = d * c
        h *= de
        if abs(de - 1.0) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - lg) * h


def chi2_isf(p: float, dof: int) -> float:
    """``x`` with ``P(chi2_dof > x) = p``, by bisection on :func:`_gammaq`."""
    lo, hi = 0.0, float(dof)
    while _gammaq(dof / 2.0, hi / 2.0) > p:
        hi *= 2.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _gammaq(dof / 2.0, mid / 2.0) > p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --- fixed point -----------------------------------------------------------------------------------


def requant(v, shift: int, bits: int, mode: str, fixed: bool):
    """Shift right by ``shift`` with rounding, then fit ``bits`` signed bits by ``mode``.

    In float (``fixed=False``) only the scaling is applied: no rounding, no overflow.
    """
    if not fixed:
        return np.asarray(v, dtype=float) / float(2 ** shift)
    v = np.asarray(v, dtype=np.int64)
    if shift > 0:
        v = (v + (1 << (shift - 1))) >> shift
    hi, lo = (1 << (bits - 1)) - 1, -(1 << (bits - 1))
    if mode == "saturate":
        return np.clip(v, lo, hi)
    if mode == "wrap":
        return ((v - lo) & ((1 << bits) - 1)) + lo
    raise ValueError(f"mode must be 'saturate' or 'wrap', got {mode!r}")


def _auto_shift(noise_value: float, bits: int, headroom: int) -> int:
    """The right shift that puts ``noise_value`` ``headroom`` bits below a ``bits``-bit full scale."""
    return max(0, math.ceil(math.log2(max(noise_value, 1.0))) - (bits - 1 - headroom))


# --- the golden ------------------------------------------------------------------------------------


@dataclass
class GoldenConfig:
    """The detector's programmable parameters (``param.md``: *Algorithm*, *Number formats*)."""

    n_h: int = 4
    n_v: int = 4
    C: int = 8
    B: int = 256
    # the direction grid: L_phi azimuths x L_eps elevations, at the centres of equal cells
    L_phi: int = 32
    L_eps: int = 8
    phi_max_deg: float = 60.0
    eps_range_deg: tuple[float, float] = (-15.0, 0.0)
    # the input and its calibrated noise level
    in_bits: int = 16
    noise_lsb: float = 4.0
    # thresholds, as targets
    pfa_gate: float = 1e-3        # a.1: search gate false alarms per block
    pfa_detect: float = 1e-4      # a.4: false detections per search (union bound over the grid)
    depart_gate_ratio: float = 1.0  # b.1: the departure energy threshold, relative to a.1's
    gamma_stop: float = 0.5       # b.2: stop when the array-SNR estimate falls below this (linear)
    # number formats
    fixed: bool = True
    mode: str = "saturate"
    u_bits: int = 16
    q_bits: int = 18
    q_headroom: int = 10
    p_bits: int = 24
    p_headroom: int = 14
    y_bits: int = 18
    y_headroom: int = 8
    pb_bits: int = 24
    pb_headroom: int = 14

    @property
    def n_rx(self) -> int:
        return self.n_h * self.n_v

    @property
    def L(self) -> int:
        return self.L_phi * self.L_eps

    @property
    def N0(self) -> float:
        """The calibrated noise power per complex sample, in LSB²: thermal plus rounding."""
        return self.noise_lsb ** 2 + 1.0 / 6.0


@dataclass
class Decision:
    """A result message: an arrival detected or a departure declared."""

    channel: int
    block: int
    kind: str                     # "arrival" or "departure"
    ell: int = -1                 # the grid index (arrival)
    phi: float = float("nan")     # rad
    eps: float = float("nan")     # rad
    gamma_hat: float = float("nan")  # the array-SNR estimate, linear (arrival)


@dataclass
class AoaGolden:
    """Every channel's search / track state machine, run without loss on blocks of integer samples."""

    cfg: GoldenConfig = field(default_factory=GoldenConfig)

    def __post_init__(self) -> None:
        c = self.cfg
        if c.B & (c.B - 1):
            raise ValueError(f"B must be a power of two, got {c.B}")
        self.grid_phi, self.grid_eps = self._grid()
        a = steering(self.grid_phi, self.grid_eps, c.n_h, c.n_v)        # (L, n_rx)
        amp = float(2 ** (c.u_bits - 1) - 1)
        if c.fixed:
            self.Ur = np.rint(a.real * amp).astype(np.int64)
            self.Ui = np.rint(a.imag * amp).astype(np.int64)
        else:
            self.Ur, self.Ui = a.real * amp, a.imag * amp
        self.norm2 = (self.Ur.astype(float) ** 2 + self.Ui.astype(float) ** 2).sum(axis=1)  # (L,)
        self._set_shifts()
        self._set_thresholds()
        self.state = np.zeros(c.C, dtype=bool)          # True: tracking an emitter
        self.track = np.full(c.C, -1, dtype=np.int64)   # the tracked grid index
        self.decisions: list[Decision] = []
        self.counts = {"blocks": 0, "gate": 0, "searches": 0, "checks": 0}

    def _grid(self):
        c = self.cfg
        pm = math.radians(c.phi_max_deg)
        e0, e1 = (math.radians(v) for v in c.eps_range_deg)
        phis = -pm + (np.arange(c.L_phi) + 0.5) * (2 * pm / c.L_phi)
        epss = e0 + (np.arange(c.L_eps) + 0.5) * ((e1 - e0) / c.L_eps)
        P, E = np.meshgrid(phis, epss, indexing="ij")
        return P.ravel(), E.ravel()

    def _set_shifts(self) -> None:
        c = self.cfg
        amp2 = float(2 ** (c.u_bits - 1) - 1) ** 2
        self.q_shift = _auto_shift(c.B * c.N0, c.q_bits, c.q_headroom)
        p_noise = amp2 * c.n_rx * c.B * c.N0 / 2 ** self.q_shift
        self.p_shift = _auto_shift(p_noise, c.p_bits, c.p_headroom)
        self.y_shift = _auto_shift(math.sqrt(amp2 * c.n_rx * c.N0), c.y_bits, c.y_headroom)
        pb_noise = c.B * amp2 * c.n_rx * c.N0 / 2 ** (2 * self.y_shift)
        self.pb_shift = _auto_shift(pb_noise, c.pb_bits, c.pb_headroom)

    def _set_thresholds(self) -> None:
        """The four thresholds, in the units each comparison sees (``algo.md``, *The thresholds*)."""
        c = self.cfg
        dof_e = 2 * c.B * c.n_rx
        self.tau_e = c.N0 / 2.0 * chi2_isf(c.pfa_gate, dof_e)            # on Σ|x|², LSB²
        self.tau_e_depart = self.tau_e * c.depart_gate_ratio
        # a.4: uᴴQ̂u · 2B / N0 ~ chi2(2B) per direction under noise; p_stored = norm2 · B · uᴴQ̂u / 2^(qs+ps)
        self.p_scale = self.norm2 * c.B / 2.0 ** (self.q_shift + self.p_shift)   # (L,)
        x = chi2_isf(c.pfa_detect / c.L, 2 * c.B) / (2 * c.B)
        self.tau_p = self.p_scale * c.N0 * x                              # (L,), stored units
        # b.2: pb_stored = norm2 · Σ|uᴴx|² / 2^(2ys+pbs);  γ̂ = Σ|uᴴx|² / (B N0) − 1
        self.pb_scale = self.norm2 * c.B / 2.0 ** (2 * self.y_shift + self.pb_shift)  # (L,)
        self.tau_pb = self.pb_scale * c.N0 * (1.0 + c.gamma_stop)         # (L,), stored units

    # -- the stages --------------------------------------------------------------------------------
    def energy(self, re: np.ndarray, im: np.ndarray) -> np.ndarray:
        """a.1 / b.1: Σ|x|² per channel over the block (exact), shape ``(C,)``."""
        if self.cfg.fixed:
            return (re * re + im * im).sum(axis=(1, 2))
        re, im = re.astype(float), im.astype(float)
        return (re * re + im * im).sum(axis=(1, 2))

    def search(self, xr: np.ndarray, xi: np.ndarray):
        """a.2 + a.3 on one channel's block ``(B, n_rx)``: the stored scan powers ``p``, shape ``(L,)``."""
        c, f = self.cfg, self.cfg.fixed
        if not f:
            xr, xi = xr.astype(float), xi.astype(float)
        # Q = X Xᴴ with X = xᵀ (n_rx x B): Qr = XrXrᵀ + XiXiᵀ, Qi = XiXrᵀ − XrXiᵀ
        Qr = xr.T @ xr + xi.T @ xi
        Qi = xi.T @ xr - xr.T @ xi
        Qr = requant(Qr, self.q_shift, c.q_bits, c.mode, f)
        Qi = requant(Qi, self.q_shift, c.q_bits, c.mode, f)
        # W = Q U (U: n_rx x L);  p_l = Re(u_lᴴ w_l) = Σ Ur Wr + Ui Wi
        Ur, Ui = self.Ur.T, self.Ui.T
        Wr = Qr @ Ur - Qi @ Ui
        Wi = Qr @ Ui + Qi @ Ur
        p = (Ur * Wr + Ui * Wi).sum(axis=0)
        return requant(p, self.p_shift, c.p_bits, c.mode, f)

    def depart_stat(self, xr: np.ndarray, xi: np.ndarray, ell: int):
        """b.2 on one channel's block for grid direction ``ell``: the stored ``pb``."""
        c, f = self.cfg, self.cfg.fixed
        if not f:
            xr, xi = xr.astype(float), xi.astype(float)
        ar, ai = self.Ur[ell], self.Ui[ell]
        yr = xr @ ar + xi @ ai          # Re(aᴴx) per sample
        yi = xi @ ar - xr @ ai          # Im(aᴴx)
        yr = requant(yr, self.y_shift, c.y_bits, c.mode, f)
        yi = requant(yi, self.y_shift, c.y_bits, c.mode, f)
        pb = (yr * yr + yi * yi).sum()
        return requant(pb, self.pb_shift, c.pb_bits, c.mode, f)

    def gamma_hat(self, p_stored, ell: int) -> float:
        """The array-SNR estimate from a stored scan power: ``uᴴQ̂u / N0 − 1``."""
        return float(p_stored) / (float(self.p_scale[ell]) * self.cfg.N0) - 1.0

    # -- one block ---------------------------------------------------------------------------------
    def step(self, block: int, re: np.ndarray, im: np.ndarray) -> list[Decision]:
        """Run every channel on one block ``(C, B, n_rx)``; return this block's decisions."""
        c = self.cfg
        e = self.energy(re, im)
        out: list[Decision] = []
        for j in range(c.C):
            if not self.state[j]:
                if e[j] < self.tau_e:
                    continue
                self.counts["gate"] += 1
                self.counts["searches"] += 1
                p = self.search(re[j], im[j])
                ell = int(np.argmax(p / self.p_scale))    # a.3: the direction of the largest uᴴQ̂u
                if p[ell] >= self.tau_p[ell]:             # a.4: the detection test
                    self.state[j], self.track[j] = True, ell
                    out.append(Decision(j, block, "arrival", ell, float(self.grid_phi[ell]),
                                        float(self.grid_eps[ell]), self.gamma_hat(p[ell], ell)))
            else:
                if e[j] >= self.tau_e_depart:
                    continue
                self.counts["checks"] += 1
                ell = int(self.track[j])
                pb = self.depart_stat(re[j], im[j], ell)
                if pb < self.tau_pb[ell]:
                    self.state[j], self.track[j] = False, -1
                    out.append(Decision(j, block, "departure", ell))
        self.counts["blocks"] += 1
        self.decisions.extend(out)
        return out

    def run(self, blocks) -> list[Decision]:
        """Run on an iterable of ``(block, re, im)`` (e.g. :meth:`Workload.blocks`)."""
        for n, re, im in blocks:
            self.step(n, re, im)
        return self.decisions
