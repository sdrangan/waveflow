"""timing.py -- the long-term update's time: three sector solves on the engine(s), then the A53.

``plans/mimo_cg/ltbf_cg_dse.md`` step 5.1.  A design is an engine build (``Knobs``) plus how it is
run: ``t`` iterations, the regime, ``units`` engines (sectors spread over them), and where the
eigendecomposition step runs:

* ``"a53"`` -- the engine hands ``A_i`` to the A53, which forms ``M_i``, runs Jacobi and forms
  ``G_i`` (the calibrated model, ``ltbf_eig.py``);
* ``"pl"`` -- the engine also forms ``M_i = H_i^H A_i`` and ``G_i = (A_i Phi)^H`` on its R x C
  array (modeled with the same structural tile count, not built) and the A53 runs Jacobi only.

Per sector, from each drop's power control at the first snapshot: ``n_conn`` connected UEs (``H``
has ``8 n_conn`` columns) and ``n_own`` own UEs.  Regime 1 solves ``8 n_own`` right-hand sides,
regime 2 ``N_rx``; either way in jobs of at most ``mmax`` columns, the first loading ``H``.  The
A53's UEs are spread over its 4 cores.  Engine and A53 are taken in sequence (no overlap): a
conservative time.  Cycles: the structural count less the per-tile correction fitted on the XSI
anchors (``hw/rtl_anchor.py``), at 4 ns.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import cache

import numpy as np

from examples.ltbf_cg import ltbf_eig as le
from examples.ltbf_cg import ltbf_link as lk
from examples.ltbf_cg import scenario as sc
from examples.ltbf_cg.hw import engine as eg
from examples.ltbf_cg.hw import models as md
from examples.ltbf_cg.hw import space as sp

CLOCK_NS = 4.0
#: DDR4-2400 x 64 at 70 % efficiency (the RFSoC 4x2's PS DDR), bits per second.
DDR_BPS = 2400e6 * 64 * 0.7
A53_HZ = 1.2e9
A53_CORES = 4
#: Typical Jacobi work per UE (the calibration corpus: 122-156 rotations, 5-6 sweeps).
N_ROT, N_SWEEPS = 140.0, 6.0
COUNTS = eg.HERE.parent / "results" / "sector_counts.json"
ARRAY_N = {64: 8, 256: 16, 1024: 32}


@dataclass(frozen=True)
class Run:
    t: int
    regime: int
    units: int
    eig: str  # "a53" | "pl"
    r: int = 1


def sector_counts(
    band: str, target: str, nmax: int, drops=range(1, 101)
) -> list[list[tuple]]:
    """Per drop, per sector: (n_conn, n_own) at the first snapshot (cached)."""
    key = f"{band}|{target}|{nmax}"
    cache_ = json.loads(COUNTS.read_text()) if COUNTS.is_file() else {}
    if key not in cache_:
        out = []
        for d in drops:
            h0 = lk.channel(sc.load_drop(d), 0, ARRAY_N[nmax], band)
            pc = lk.power_control(h0, lk.TARGETS[target])
            n_conn = int(pc.iconn.size)
            out.append(
                [
                    (n_conn, int(np.sum(pc.gnb_idx[pc.iconn] == s)))
                    for s in range(sc.N_SECT)
                ]
            )
        cache_[key] = out
        COUNTS.parent.mkdir(parents=True, exist_ok=True)
        COUNTS.write_text(json.dumps(cache_))
    return [[tuple(x) for x in drop] for drop in cache_[key]]


def sector_counts_nue(nue: int, groups=range(1, 26)) -> list[list[tuple]]:
    """The N_UE sweep's per-group sector populations (16 x 16, 100 MHz, [-6, 3] dB; cached)."""
    from examples.ltbf_cg import ltbf_accuracy_sweep as sw

    key = f"nue|{nue}"
    cache_ = json.loads(COUNTS.read_text()) if COUNTS.is_file() else {}
    if key not in cache_:
        out = []
        for g in groups:
            job = sw.Job("100MHz", "-6_3", "10dB", 16, nue, g)
            pc = lk.power_control(sw.channels(job, 0), lk.TARGETS["-6_3"])
            out.append(
                [
                    (int(pc.iconn.size), int(np.sum(pc.gnb_idx[pc.iconn] == s)))
                    for s in range(sc.N_SECT)
                ]
            )
        cache_[key] = out
        COUNTS.write_text(json.dumps(cache_))
    return [[tuple(x) for x in grp] for grp in cache_[key]]


@cache
def a53_params() -> dict:
    return json.loads((le.DATA / "model.json").read_text())["params"]


#: A modeled optimized A53 kernel (vectorized, contraction on): the ``"a53x4"`` placement prices
#: the calibrated scalar kernel's n-proportional work 4 x faster -- a sensitivity, not calibrated.
A53_SPEEDUP = {"a53": 1.0, "a53x4": 4.0, "pl": 1.0}


def a53_counters(srs: str) -> tuple[float, float]:
    """Typical Jacobi rotations and sweeps per UE for an SRS setting: the mean over the served UEs
    of drops 1-3 at 16 x 16, 100 MHz, [-6, 3] dB, exact LTBF (cached).  A noiseless estimate's
    M_i is nearly rank one and needs a few rotations; a noisy one needs about 140."""
    from examples.ltbf_cg import ltbf as lt
    from examples.ltbf_cg import ltbf_eig as le_

    key = f"jacobi|{srs}"
    cache_ = json.loads(COUNTS.read_text()) if COUNTS.is_file() else {}
    if key not in cache_:
        g = None if srs == "none" else lk.G_SRS_DB
        rot, sw = [], []
        for d in (1, 2, 3):
            h0 = lk.channel(sc.load_drop(d), 0, 16, "100MHz")
            pc = lk.power_control(h0, lk.TARGETS["-6_3"])
            ltu = lk.long_term(lk.scaled(h0, pc), pc, np.random.default_rng(d), g)
            for s_ in range(sc.N_SECT):
                for u, a_i in lt.sector_a(ltu, s_, lt.Solve(), None).items():
                    h_i = ltu.srs_own[u]
                    m = h_i.conj().T @ a_i
                    m = 0.5 * (m + m.conj().T)
                    _, n_rot, n_sw = le_._jacobi(
                        [[[float(v.real), float(v.imag)] for v in row] for row in m]
                    )
                    rot.append(n_rot)
                    sw.append(n_sw)
        cache_[key] = [float(np.mean(rot)), float(np.mean(sw))]
        COUNTS.write_text(json.dumps(cache_))
    return tuple(cache_[key])


def a53_ue_cycles(
    n: int, r: int, eig: str, counters: tuple[float, float] = (N_ROT, N_SWEEPS)
) -> float:
    """One UE's A53 cycles: Jacobi always; M_i and G_i too unless the engine forms them."""
    p = a53_params()
    c0, (cn, cnr, crot, csw) = p["intercept"], p["coeffs"]
    jac = c0 + crot * counters[0] + csw * counters[1]
    return jac if eig == "pl" else jac + (cn * n + cnr * n * r) / A53_SPEEDUP[eig]


def pl_eig_cycles(k: sp.Knobs, o: md.Overheads, n: int, r: int) -> float:
    """M_i (8 x 8 over n) and G_i (r x n over 8) on the engine's tile, per UE (modeled)."""
    a = o.a_T if np.isfinite(o.a_T) else o.a_S
    b = o.a_U if np.isfinite(o.a_U) else o.a_S
    return math.ceil(8 / k.R) * math.ceil(8 / k.C) * (n + a) + math.ceil(
        n / k.R
    ) * math.ceil(r / k.C) * (8 + b)


def regime2_a_cycles(k: sp.Knobs, o: md.Overheads, n: int) -> float:
    """Regime 2's A_i = X_sym H_i (n x n times n x 8) on the engine's tile, per UE (modeled; the
    symmetrization reads X and its transpose)."""
    a = o.a_T if np.isfinite(o.a_T) else o.a_S
    return math.ceil(n / k.R) * math.ceil(8 / k.C) * (n + a)


def io_cycles(n: int, cols: int, m: int, load_h: bool) -> float:
    """The stream words of one job at one complex value per beat (job_cycles' I/O terms)."""
    return (n * cols if load_h else 0) + 2 * n * m


def sector_cycles(
    k: sp.Knobs,
    o: md.Overheads,
    n_conn: int,
    n_own: int,
    regime: int,
    eig: str,
    delta: float,
    L: int = 1,
    r: int = 1,
) -> tuple[float, float]:
    """One sector's engine cycles as ``(fixed, per iteration)``: jobs of <= ``mmax`` columns padded
    to the tile (the first loads H), I/O at ``L`` complex values per beat, regime 2's A_i, and M_i,
    G_i when the engine forms them.  Explicit at N_rx > 256 streams Q from DDR each S pass.
    """
    if n_own == 0:
        return 0.0, 0.0
    n = k.nmax
    cols = 8 * n_conn
    cols_p = math.ceil(cols / k.R) * k.R
    if cols_p > k.cmax:
        raise ValueError(f"{cols} columns exceed the build's cmax {k.cmax}")
    m_total = 8 * n_own if regime == 1 else n
    c0 = c1 = 0.0
    first = True
    for j in range(math.ceil(m_total / k.mmax)):
        m = min(k.mmax, m_total - j * k.mmax)
        m_p = math.ceil(m / k.C) * k.C
        a = md.job_cycles_rtl(k, o, n, cols_p, m_p, 0, first, delta)
        b = md.job_cycles_rtl(k, o, n, cols_p, m_p, 1, first, delta) - a
        io = io_cycles(n, cols_p, m_p, first)
        a += math.ceil(io / L) - io
        if k.explicit and n > sp.EXPLICIT_NMAX:  # each S pass streams Q from DDR
            b = max(
                b, math.ceil(m_p / k.C) * 2 * k.W * n * n / DDR_BPS / (CLOCK_NS * 1e-9)
            )
        c0, c1 = c0 + a, c1 + b
        first = False
    if regime == 2:
        c0 += n_own * regime2_a_cycles(k, o, n)
    if eig == "pl":
        c0 += n_own * pl_eig_cycles(k, o, n, r)
    return c0, c1


def assign(cyc: np.ndarray, units: int) -> np.ndarray:
    """Longest-first assignment of the three sectors (rows) to ``units`` engines: the makespan."""
    srt = -np.sort(-cyc, axis=0)
    if units >= 3:
        return srt[0]
    if units == 2:
        return np.maximum(srt[0], srt[1] + srt[2])
    return srt.sum(axis=0)


def update_time_s(
    k: sp.Knobs,
    o: md.Overheads,
    run: Run,
    sectors: list[tuple[int, int]],
    delta: float,
    L: int = 1,
    counters: tuple[float, float] = (N_ROT, N_SWEEPS),
) -> float:
    """One long-term update (all three sectors) for one drop's sector populations."""
    cyc = np.array(
        [[sum(sector_cycles(k, o, nc, no, run.regime, run.eig, delta, L, run.r) * np.array([1, run.t]))]
         for nc, no in sectors]
    )  # fmt: skip
    engine_s = float(assign(cyc, run.units)[0]) * CLOCK_NS * 1e-9
    n_ue = sum(no for _, no in sectors)
    a53_s = (
        math.ceil(n_ue / A53_CORES)
        * a53_ue_cycles(k.nmax, run.r, run.eig, counters)
        / A53_HZ
    )
    return engine_s + a53_s
