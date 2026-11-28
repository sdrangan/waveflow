"""score.py -- the accuracy metrics: result messages against the truth log.

``docs/examples/aoa_detect/problem.md``, *What is measured*.  Delays are in **blocks** here: the golden
decides at once, so its delay counts only the blocks that pass before a decision.  The timed models add
the decision time within a block.

* **missed detection**: an emitter with no arrival decision in its channel while it is on;
* **false alarm**: an arrival decision in a channel with no emitter on at that block;
* **early stop**: a departure decision while the channel's emitter is still on;
* **angular error**: the angle between the decided and the true direction, at each emitter's first
  arrival decision; also the azimuth and elevation errors, and the SNR error.
"""
from __future__ import annotations

from dataclasses import dataclass, field

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
        if not arr:
            s.missed += 1
            continue
        s.detected += 1
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
