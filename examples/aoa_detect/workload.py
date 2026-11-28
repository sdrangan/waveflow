"""workload.py -- the synthetic workload: emitters, their on/off process, and the receiver's samples.

``docs/examples/aoa_detect/workload.md``.  A :class:`Workload` generates every channel's samples block
by block from a seed, quantized to the input format, with a **truth log** of every emitter (its
channel, first and last block, direction and SNR) for scoring.

**Emitters** are placed on the ground uniformly over one sector's area, their direction from the
geometry and their per-element SNR from free-space path loss (Friis), the transmit power, the 3GPP
sector element and the receiver's noise (``mode="geometric"``); or, as a check, with the SNR uniform in
dB and the direction uniform over the sector (``mode="statistical"``).  Each channel's emitter switches
on and off as a Gilbert-Elliott process stepped once per block, with a burst trigger that turns
``k_burst`` idle channels on together.  A **script** of emitters replaces the random process for
deterministic scenarios.

**The samples.**  For channel ``j``, ``x = sqrt(g_el) a(theta) s + w`` with ``s ~ CN(0, 1)``,
``w ~ CN(0, I)`` and ``a`` the unit-modulus steering vector (:mod:`.array`).  The **input gain** scales
them so that the noise has complex standard deviation ``noise_lsb`` LSBs, then they are rounded and
saturated to ``in_bits`` bits per real and imaginary part.  See :func:`input_gain_tradeoff` for what
``noise_lsb`` trades.

**Timing of the process.**  At block ``n``, each channel moves from its state at ``n - 1``: an emitter on
at ``n - 1`` ends with probability ``p_off`` (its last block is ``n - 1``); a channel idle at ``n - 1``
starts one with probability ``p_on``, and with probability ``p_burst`` up to ``k_burst`` channels idle at
``n - 1`` start together.  A channel whose emitter ends at ``n`` stays idle at ``n``, so a channel is busy
``p_on / (p_on + p_off)`` of the time.  Then block ``n``'s samples.  An emitter is on for blocks
``[start, end)``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from examples.aoa_detect.array import element_gain_db, steering

C_LIGHT = 299_792_458.0


@dataclass
class WorkloadConfig:
    """Every parameter of the workload (``docs/examples/aoa_detect/param.md``, *Workload*)."""

    # the receiver and the stream
    n_h: int = 4
    n_v: int = 4
    C: int = 8
    B: int = 256
    fs: float = 30.72e6
    fc: float = 3.5e9
    nf_db: float = 7.0
    in_bits: int = 16
    noise_lsb: float = 4.0          # the input gain: the noise's complex std, in LSBs
    # the geometry
    mode: str = "geometric"         # "geometric" or "statistical"
    h_rx: float = 25.0
    tilt_deg: float = 6.0
    r_min: float = 100.0
    r_max: float = 5000.0
    phi_max_deg: float = 60.0
    h_e: float = 1.5
    pt_dbm: tuple[float, float] = (5.0, 30.0)
    snr_db_range: tuple[float, float] = (-20.0, 30.0)  # statistical mode: per-element SNR
    eps_range_deg: tuple[float, float] = (-13.0, -0.5)  # statistical mode: elevation
    # the on/off process
    # about 1.5 activations a block over C = 8 channels: emitters of about 5 blocks, half the time on
    p_on: float = 3 / 16
    p_off: float = 3 / 16
    p_burst: float = 1 / 50
    k_burst: int = 4
    seed: int = 0

    @property
    def n_rx(self) -> int:
        return self.n_h * self.n_v

    @property
    def noise_dbm(self) -> float:
        return -174.0 + 10.0 * np.log10(self.fs) + self.nf_db


@dataclass
class Emitter:
    """One emitter of the truth log.  On for blocks ``[start, end)``; ``end`` is ``None`` while on."""

    channel: int
    start: int
    end: int | None
    phi: float          # azimuth, rad
    eps: float          # elevation, rad (negative below the horizon)
    snr_db: float       # per-element SNR, dB
    range_m: float = float("nan")
    pt_dbm: float = float("nan")

    def on_at(self, block: int) -> bool:
        return self.start <= block and (self.end is None or block < self.end)


def accuracy_workload(**kw) -> WorkloadConfig:
    """Phase 1, untimed: the statistical mode, SNR uniform in dB over [-20, 30] per element, so every
    1 dB of the detection curve gets the same number of emitters (``workload.md``)."""
    return WorkloadConfig(**{"mode": "statistical", **kw})


def timing_workload(**kw) -> WorkloadConfig:
    """Phase 2, timed: the geometric mode, emitters out to 5 km, so the realistic mix of SNRs and
    directions includes emitters near the detection limit."""
    return WorkloadConfig(**{"mode": "geometric", **kw})


def place_emitter(rng: np.random.Generator, cfg: WorkloadConfig) -> dict:
    """Draw one emitter's direction and per-element SNR (``workload.md``, *The geometry*)."""
    phi_max = np.radians(cfg.phi_max_deg)
    if cfg.mode == "statistical":
        return {"phi": rng.uniform(-phi_max, phi_max),
                    "eps": np.radians(rng.uniform(*cfg.eps_range_deg)),
                    "snr_db": rng.uniform(*cfg.snr_db_range)}
    if cfg.mode != "geometric":
        raise ValueError(f"mode must be 'geometric' or 'statistical', got {cfg.mode!r}")
    r = np.sqrt(rng.uniform(cfg.r_min ** 2, cfg.r_max ** 2))     # uniform over the area
    phi = rng.uniform(-phi_max, phi_max)
    pt = rng.uniform(*cfg.pt_dbm)
    dz = cfg.h_rx - cfg.h_e
    d = np.hypot(r, dz)
    eps = -np.arctan2(dz, r)
    lam = C_LIGHT / cfg.fc
    fspl = 20.0 * np.log10(4.0 * np.pi * d / lam)
    g = element_gain_db(phi, eps, np.radians(cfg.tilt_deg))
    return {"phi": phi, "eps": eps, "snr_db": pt - fspl + g - cfg.noise_dbm, "range_m": r, "pt_dbm": pt}


def input_gain_tradeoff(in_bits: int, noise_lsb: float) -> dict:
    """What the input gain trades, for noise of complex std ``noise_lsb`` LSBs at ``in_bits`` bits.

    * ``quant_db``: the rounding noise (``1/6`` LSB² per complex sample) against the thermal noise.
    * ``headroom_db``: the largest per-element SNR whose per-real-part std stays within a quarter of
      full scale (clipping beyond four standard deviations, about one sample in 16,000).
    * ``noise_dbfs``: the thermal noise's power against a full-scale complex sine.
    """
    fs = 2.0 ** (in_bits - 1)
    s2 = float(noise_lsb) ** 2
    g = 2.0 * (fs / 4.0) ** 2 / s2 - 1.0
    return {"quant_db": 10.0 * np.log10((1.0 / 6.0) / s2),
                "headroom_db": 10.0 * np.log10(g),
                "noise_dbfs": 10.0 * np.log10(s2 / (2.0 * fs ** 2))}


@dataclass
class Workload:
    """The block-by-block source.  ``script`` (a list of :class:`Emitter`) replaces the random process."""

    cfg: WorkloadConfig = field(default_factory=WorkloadConfig)
    script: list[Emitter] | None = None

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.cfg.seed)
        self.block = 0
        self.truth: list[Emitter] = []
        self._active: list[Emitter | None] = [None] * self.cfg.C
        self.clipped = np.zeros(self.cfg.C, dtype=np.int64)   # clipped real or imaginary parts
        if self.script is not None:
            self.truth = [Emitter(**vars(e)) for e in self.script]

    # -- the on/off process ------------------------------------------------------------------------
    def _new(self, j: int, n: int) -> Emitter:
        e = Emitter(channel=j, start=n, end=None, **place_emitter(self.rng, self.cfg))
        self.truth.append(e)
        return e

    def _step(self, n: int) -> list[Emitter | None]:
        cfg = self.cfg
        if self.script is not None:
            on = [None] * cfg.C
            for e in self.truth:
                if e.on_at(n):
                    if on[e.channel] is not None:
                        raise ValueError(f"script: two emitters in channel {e.channel} at block {n}")
                    on[e.channel] = e
            return on
        was_off = [e is None for e in self._active]   # each transition from block n - 1's state
        for j, e in enumerate(self._active):          # off-transitions
            if e is not None and self.rng.random() < cfg.p_off:
                e.end = n
                self._active[j] = None
        for j in range(cfg.C):                        # on-transitions
            if was_off[j] and self.rng.random() < cfg.p_on:
                self._active[j] = self._new(j, n)
        if self.rng.random() < cfg.p_burst:           # the burst trigger
            idle = [j for j in range(cfg.C) if was_off[j] and self._active[j] is None]
            for j in self.rng.permutation(idle)[: cfg.k_burst]:
                self._active[int(j)] = self._new(int(j), n)
        return list(self._active)

    # -- the samples -------------------------------------------------------------------------------
    def next_block(self) -> tuple[np.ndarray, np.ndarray]:
        """Block ``self.block``'s samples, as integer ``(re, im)`` arrays of shape ``(C, B, n_rx)``."""
        cfg = self.cfg
        n = self.block
        on = self._step(n)
        shape = (cfg.C, cfg.B, cfg.n_rx)
        x = (self.rng.standard_normal(shape) + 1j * self.rng.standard_normal(shape)) / np.sqrt(2.0)
        for j, e in enumerate(on):
            if e is None:
                continue
            a = steering(e.phi, e.eps, cfg.n_h, cfg.n_v)
            s = (self.rng.standard_normal(cfg.B) + 1j * self.rng.standard_normal(cfg.B)) / np.sqrt(2.0)
            x[j] += np.sqrt(10.0 ** (e.snr_db / 10.0)) * s[:, None] * a[None, :]
        re, im = self._quantize(x * cfg.noise_lsb)
        self.block += 1
        return re, im

    def _quantize(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        hi = 2 ** (self.cfg.in_bits - 1) - 1
        lo = -(2 ** (self.cfg.in_bits - 1))
        out = []
        for part in (x.real, x.imag):
            q = np.rint(part).astype(np.int64)
            self.clipped += ((q > hi) | (q < lo)).sum(axis=(1, 2))
            out.append(np.clip(q, lo, hi))
        return out[0], out[1]

    def blocks(self, n_blocks: int):
        """Yield ``(block_index, re, im)`` for the next ``n_blocks`` blocks."""
        for _ in range(int(n_blocks)):
            n = self.block
            re, im = self.next_block()
            yield n, re, im

    def finish(self) -> list[Emitter]:
        """The truth log so far; emitters still on keep ``end = None``."""
        return self.truth
