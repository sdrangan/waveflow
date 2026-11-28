"""score.py -- the accuracy metrics: result messages against the truth log.

``docs/examples/aoa_detect/problem.md``, *What is measured*.  Delays are in **blocks** here: the golden
decides at once, so its delay counts only the blocks that pass before a decision.  The timed models add
the decision time within a block.

* **missed detection**: an emitter with no arrival decision in its channel while it is on;
* **false alarm**: an arrival decision in a channel with no emitter on at that block;
* **early stop**: a departure decision while the channel's emitter is still on;
* **angular error**: the angle between the decided and the true direction, at each emitter's first
  arrival decision; also the azimuth and elevation errors, and the SNR error.

Since the workload spans SNRs below the detection limit, misses cannot be zero.  Detection is scored
as a **curve**, the fraction detected against the per-element SNR (:func:`detection_curve`), and one
number from it, the **SNR at 90% detection** (:func:`snr_at_pd`): a loss from the formats or the timing
is a shift in dB.  :func:`loss_split` attributes each miss to its cause by comparing runs on the same
samples: **inherent** (missed by the float golden), **format** (detected in float, missed in fixed
point) and **timing** (detected by the fixed-point golden, missed by the timed system).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

import numpy as np


@dataclass
class Score:
    emitters: int = 0
    detected: int = 0
    missed: int = 0
    false_alarms: int = 0
    early_stops: int = 0
    detect_delay_blocks: list[int] = field(default_factory=list)
    depart_delay_blocks: list[int] = field(default_factory=list)
    angle_err_deg: list[float] = field(default_factory=list)
    az_err_deg: list[float] = field(default_factory=list)
    el_err_deg: list[float] = field(default_factory=list)
    snr_err_db: list[float] = field(default_factory=list)
    # per scored emitter, in truth order: its per-element SNR, and whether it was detected
    emitter_snr_db: list[float] = field(default_factory=list)
    emitter_detected: list[bool] = field(default_factory=list)
    # per detected emitter: its SNR (for the angular error against SNR)
    detected_snr_db: list[float] = field(default_factory=list)


def _angle_between(phi1, eps1, phi2, eps2) -> float:
    def unit(p, e):
        return np.array([np.cos(e) * np.cos(p), np.cos(e) * np.sin(p), np.sin(e)])
    c = float(np.clip(unit(phi1, eps1) @ unit(phi2, eps2), -1.0, 1.0))
    return float(np.degrees(np.arccos(c)))


def score(decisions, truth, n_blocks: int, n_rx: int) -> Score:
    """Score ``decisions`` (:class:`~examples.aoa_detect.golden.Decision`) against ``truth``
    (:class:`~examples.aoa_detect.workload.Emitter`) over blocks ``[0, n_blocks)``."""
    s = Score()
    by_ch: dict[int, list] = {}
    for e in truth:
        by_ch.setdefault(e.channel, []).append(e)

    def emitter_at(ch, b):
        for e in by_ch.get(ch, []):
            if e.on_at(b):
                return e
        return None

    for d in decisions:
        e = emitter_at(d.channel, d.block)
        if d.kind == "arrival" and e is None:
            s.false_alarms += 1
        elif d.kind == "departure" and e is not None:
            s.early_stops += 1

    for e in truth:
        if e.start >= n_blocks:
            continue
        s.emitters += 1
        end = n_blocks if e.end is None else min(e.end, n_blocks)
        arr = [d for d in decisions
               if d.channel == e.channel and d.kind == "arrival" and e.start <= d.block < end]
        s.emitter_snr_db.append(e.snr_db)
        s.emitter_detected.append(bool(arr))
        if not arr:
            s.missed += 1
            continue
        s.detected += 1
        s.detected_snr_db.append(e.snr_db)
        d = min(arr, key=lambda x: x.block)
        s.detect_delay_blocks.append(d.block - e.start)
        s.angle_err_deg.append(_angle_between(d.phi, d.eps, e.phi, e.eps))
        s.az_err_deg.append(float(np.degrees(d.phi - e.phi)))
        s.el_err_deg.append(float(np.degrees(d.eps - e.eps)))
        g_true = n_rx * 10.0 ** (e.snr_db / 10.0)
        if d.gamma_hat > 0:
            s.snr_err_db.append(float(10.0 * np.log10(d.gamma_hat / g_true)))
        if e.end is not None and e.end < n_blocks:
            dep = [x for x in decisions if x.channel == e.channel and x.kind == "departure"
                   and x.block >= e.end]
            if dep:
                s.depart_delay_blocks.append(min(x.block for x in dep) - e.end)
    return s


def detection_curve(s: Score, lo: float = -20.0, hi: float = 30.0, step: float = 1.0):
    """The fraction detected per SNR bin: ``(centres, pd, counts)``; ``pd`` is NaN for an empty bin."""
    edges = np.arange(lo, hi + step / 2, step)
    snr = np.asarray(s.emitter_snr_db, dtype=float)
    det = np.asarray(s.emitter_detected, dtype=bool)
    idx = np.digitize(snr, edges) - 1
    n = len(edges) - 1
    counts = np.bincount(idx[(idx >= 0) & (idx < n)], minlength=n)
    hits = np.bincount(idx[(idx >= 0) & (idx < n)], weights=det[(idx >= 0) & (idx < n)], minlength=n)
    with np.errstate(invalid="ignore", divide="ignore"):
        pd = np.where(counts > 0, hits / np.maximum(counts, 1), np.nan)
    return 0.5 * (edges[:-1] + edges[1:]), pd, counts


def snr_at_pd(s: Score, target: float = 0.9) -> float:
    """The per-element SNR at which the fraction detected reaches ``target``.

    A logistic fit of detection against SNR, ``P(detect) = 1 / (1 + exp(-(a + b z)))`` with ``z`` the
    standardized SNR, by Newton's method with step halving on the likelihood, rather than reading a
    noisy binned curve.  NaN if every emitter, or none, was detected.
    """
    x = np.asarray(s.emitter_snr_db, dtype=float)
    y = np.asarray(s.emitter_detected, dtype=float)
    if len(x) < 10 or y.min() == y.max():
        return float("nan")
    mu, sd = x.mean(), x.std()
    X = np.column_stack([np.ones_like(x), (x - mu) / sd])

    def loglik(w):
        t = X @ w
        return float((y * t - np.logaddexp(0.0, t)).sum())

    w = np.zeros(2)
    ll = loglik(w)
    for _ in range(200):
        p = 0.5 * (1.0 + np.tanh(0.5 * (X @ w)))       # the logistic, without overflow
        g = X.T @ (y - p)
        H = X.T @ (X * (p * (1 - p))[:, None]) + 1e-9 * np.eye(2)
        dw = np.linalg.solve(H, g)
        step = 1.0
        while step > 1e-6 and loglik(w + step * dw) < ll:
            step /= 2.0
        w = w + step * dw
        new = loglik(w)
        if abs(new - ll) < 1e-10:
            break
        ll = new
    a, b = w
    if b <= 0:
        return float("nan")
    return float(mu + sd * (np.log(target / (1 - target)) - a) / b)


def angle_error_curve(s: Score, lo: float = -20.0, hi: float = 30.0, step: float = 5.0):
    """The median angular error per SNR bin, over detected emitters: ``(centres, median_deg, counts)``."""
    edges = np.arange(lo, hi + step / 2, step)
    snr = np.asarray(s.detected_snr_db, dtype=float)
    err = np.asarray(s.angle_err_deg, dtype=float)
    centres, med, counts = [], [], []
    for a, b in pairwise(edges):
        m = (snr >= a) & (snr < b)
        centres.append(0.5 * (a + b))
        counts.append(int(m.sum()))
        med.append(float(np.median(err[m])) if m.any() else float("nan"))
    return np.array(centres), np.array(med), np.array(counts)


@dataclass
class LossSplit:
    """Every scored emitter's misses, by cause (runs on the same samples)."""

    emitters: int = 0
    inherent: int = 0      # missed by the float golden
    format: int = 0        # detected in float, missed in fixed point
    timing: int = 0        # detected by the fixed-point golden, missed by the system
    recovered: int = 0     # missed by a reference, detected by a later stage (by chance)


def loss_split(float_score: Score, fixed_score: Score, system_score: Score | None = None) -> LossSplit:
    """Attribute misses to their cause.  Each score comes from :func:`score` on the same truth and
    samples; ``system_score`` (the timed system) defaults to the fixed-point golden itself."""
    sys_s = fixed_score if system_score is None else system_score
    f, x, t = (np.asarray(v.emitter_detected, dtype=bool) for v in (float_score, fixed_score, sys_s))
    if not (len(f) == len(x) == len(t)):
        raise ValueError("loss_split: the scores cover different emitters")
    out = LossSplit(emitters=len(f))
    out.inherent = int((~f).sum())
    out.format = int((f & ~x).sum())
    out.timing = int((x & ~t).sum())
    out.recovered = int((~f & x).sum() + (~x & t).sum())
    return out
