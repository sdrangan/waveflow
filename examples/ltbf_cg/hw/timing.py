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


@cache
def a53_params() -> dict:
    return json.loads((le.DATA / "model.json").read_text())["params"]


def a53_ue_cycles(n: int, r: int, eig: str) -> float:
    p = a53_params()
    c0, (cn, cnr, crot, csw) = p["intercept"], p["coeffs"]
    jac = c0 + crot * N_ROT + csw * N_SWEEPS
    return jac if eig == "pl" else jac + cn * n + cnr * n * r


def pl_eig_cycles(k: sp.Knobs, o: md.Overheads, n: int, r: int) -> float:
    """M_i (8 x 8 over n) and G_i (r x n over 8) on the engine's tile, per UE (modeled)."""
    m_cyc = (
        math.ceil(8 / k.R)
        * math.ceil(8 / k.C)
        * (n + (o.a_T if np.isfinite(o.a_T) else o.a_S))
    )
    g_cyc = (
        math.ceil(n / k.R)
        * math.ceil(r / k.C)
        * (8 + (o.a_U if np.isfinite(o.a_U) else o.a_S))
    )
    return m_cyc + g_cyc


def update_time_s(
    k: sp.Knobs, o: md.Overheads, run: Run, sectors: list[tuple[int, int]], delta: float
) -> float:
    """One long-term update (all three sectors) for one drop's sector populations."""
    n = k.nmax
    per_sector = []
    n_ue = 0
    for n_conn, n_own in sectors:
        if n_own == 0:
            per_sector.append(0.0)
            continue
        cols = 8 * n_conn
        m_total = 8 * n_own if run.regime == 1 else n
        cyc, first = 0.0, True
        for j in range(math.ceil(m_total / k.mmax)):
            m = min(k.mmax, m_total - j * k.mmax)
            cyc += md.job_cycles_rtl(k, o, n, cols, m, run.t, first, delta)
            first = False
        if run.eig == "pl":
            cyc += n_own * pl_eig_cycles(k, o, n, run.r)
        per_sector.append(cyc)
        n_ue += n_own
    # sectors over the units: the longest-first greedy assignment
    loads = [0.0] * run.units
    for c in sorted(per_sector, reverse=True):
        loads[loads.index(min(loads))] += c
    engine_s = max(loads) * CLOCK_NS * 1e-9
    a53_s = math.ceil(n_ue / A53_CORES) * a53_ue_cycles(n, run.r, run.eig) / A53_HZ
    return engine_s + a53_s
