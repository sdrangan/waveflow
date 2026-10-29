"""ltbf_proj.py -- the A53's job per UE in the LTBF long-term update, priced on the processor model.

``plans/ltbf_dse/ltbf_dse.md`` step 4.6 (AC10; spec §3.4, §5.6).  From the systolic engine's
``W = Ĥα'ᴴX_b`` and the stored ``X`` (each UE's 8 columns), the A53 forms ``Mᵢ = √8·W[ii]``,
eigendecomposes it by cyclic complex Jacobi (``ltbf_eig``'s), forms ``Gᵢ = (XᵢΦᵢ)ᴴ``, the other
connected UEs' cross terms ``ΦᵢᴴW[j,i]ᴴ``, ``R̃ᵢ``, its Cholesky factor and ``G'ᵢ = Lᵢ⁻¹Gᵢ``.
``cpu/ltbf_proj.c`` is that program; :func:`ltbf_proj` its bit-exact twin (outputs and work
counters); :data:`KERNEL` its calibration :class:`~waveflow.cpu.calib.kernels.Kernel` and
:data:`FAMILY` the cost model's form: cycles linear in ``n``, ``n·r`` (``G``, ``GGᴴ``, ``G'``), ``conn``,
``conn·r`` (the cross terms), the rotations and the sweeps.

The system computes the job with ``ltbf_fixed_chain.project_ue`` (numpy: the bit-exact chain) and
prices it with this model on :func:`proj_counters` of the same inputs (the Jacobi work of that UE's
own ``M``).  The campaign follows ``ltbf_eig``'s (``a53cal``): points registered in
``waveflow/cpu/calib/sweep.py`` (:func:`~waveflow.cpu.calib.sweep.ltbf_proj_rows`) and committed as
``cpu/sweep_plan_proj.csv`` before any is measured; the corpus and model local (``data/a53_ltbf/``).

    python -m examples.ltbf_dse.ltbf_proj plan | smoke | campaign | fit
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

from examples.ltbf_dse import a53cal
from examples.ltbf_dse.ltbf_eig import K, _bits, _jacobi
from waveflow.cpu.calib.calibrate import Family
from waveflow.cpu.calib.kernels import Kernel
from waveflow.cpu.calib.kernels.common import Rng

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "cpu" / "ltbf_proj.c"
PLAN = HERE / "cpu" / "sweep_plan_proj.csv"
DATA = HERE / "data" / "a53_ltbf"


def _top(m, r: int) -> list[int]:
    top = [0, 0]
    for j in range(r):
        best = -1
        for k in range(K):
            if j == 1 and k == top[0]:
                continue
            if best < 0 or m[k][k][0] > m[best][best][0]:
                best = k
        top[j] = best
    return top


def _m_block(w, pu: int) -> list:
    """``M = √8·W[pu block]``, then ``(M + Mᴴ) / 2``, as ``ltbf_proj()``."""
    sq8 = math.sqrt(8.0)
    m = [[[sq8 * w[pu * K + p][q][0], sq8 * w[pu * K + p][q][1]] for q in range(K)]
         for p in range(K)]  # fmt: skip
    for p in range(K):
        for q in range(p, K):
            re = 0.5 * (m[p][q][0] + m[q][p][0])
            im = 0.5 * (m[p][q][1] - m[q][p][1])
            m[p][q] = [re, im]
            m[q][p] = [re, -im]
    return m


def _proj(x, w, n: int, r: int, conn: int, pu: int):
    """The per-UE job of ``ltbf_proj.c``: ``(G', λ_top, n_rot, n_sweeps, degenerate)``."""
    m = _m_block(w, pu)
    v, n_rot, n_sweeps = _jacobi(m)
    top = _top(m, r)
    lam = m[top[0]][top[0]][0]

    def project(rows, nrows):
        out = []
        for j in range(r):
            row = []
            for i in range(nrows):
                re = im = 0.0
                for k in range(K):
                    ar, ai = rows[i][k]
                    br, bi = v[k][top[j]]
                    re += ar * br - ai * bi
                    im += ar * bi + ai * br
                row.append([re, -im])
            out.append(row)
        return out

    g = project(x, n)
    gh = project(w, K * conn)
    rt = [[[0.0, 0.0] for _ in range(r)] for _ in range(r)]
    for a in range(r):
        for b in range(r):
            re = im = 0.0
            for i in range(n):
                re += g[a][i][0] * g[b][i][0] + g[a][i][1] * g[b][i][1]
                im += g[a][i][1] * g[b][i][0] - g[a][i][0] * g[b][i][1]
            for row in range(K * conn):
                if row // K == pu:
                    continue
                re += gh[a][row][0] * gh[b][row][0] + gh[a][row][1] * gh[b][row][1]
                im += gh[a][row][1] * gh[b][row][0] - gh[a][row][0] * gh[b][row][1]
            rt[a][b] = [re, im]
    zero = [[[0.0, 0.0] for _ in range(n)] for _ in range(r)]
    if not rt[0][0][0] > 0.0:
        return zero, lam, n_rot, n_sweeps, 1
    l00 = math.sqrt(rt[0][0][0])
    gp = [[[g[0][i][0] / l00, g[0][i][1] / l00] for i in range(n)]]
    if r == 2:
        l10r, l10i = rt[1][0][0] / l00, rt[1][0][1] / l00
        d = rt[1][1][0] - (l10r * l10r + l10i * l10i)
        if not d > 0.0:
            return zero, lam, n_rot, n_sweeps, 1
        l11 = math.sqrt(d)
        row = []
        for i in range(n):
            re = g[1][i][0] - (l10r * gp[0][i][0] - l10i * gp[0][i][1])
            im = g[1][i][1] - (l10r * gp[0][i][1] + l10i * gp[0][i][0])
            row.append([re / l11, im / l11])
        gp.append(row)
    return gp, lam, n_rot, n_sweeps, 0


def ltbf_proj(
    n: int, r: int, conn: int = 8, rank: int = 8, seed: int = 1
) -> dict[str, Any]:
    """The twin of ``cpu/ltbf_proj.c``: its inputs, its computation, its printed JSON."""
    n, r, conn, rank = int(n), int(r), int(conn), int(rank)
    rng = Rng(int(seed))

    def draw() -> float:
        u = rng.next()
        return float(u - (1 << 32) if u & 0x80000000 else u) / 2147483648.0

    pu = conn // 2
    x = [[[draw(), draw()] for _ in range(K)] for _ in range(n)]
    w = [[[draw(), draw()] for _ in range(K)] for _ in range(K * conn)]
    uu = [[[0.0, 0.0] for _ in range(K)] for _ in range(K)]
    vv = [[[0.0, 0.0] for _ in range(K)] for _ in range(K)]
    for k in range(rank):
        for q in range(K):
            uu[k][q] = [draw(), draw()]
            vv[k][q] = [draw(), draw()]
    for p in range(K):
        for q in range(K):
            re = im = 0.0
            for k in range(rank):
                re += uu[k][p][0] * vv[k][q][0] + uu[k][p][1] * vv[k][q][1]
                im += uu[k][p][0] * vv[k][q][1] - uu[k][p][1] * vv[k][q][0]
            hre = re + draw() * 0.0009765625
            him = im + draw() * 0.0009765625
            w[pu * K + p][q] = [hre, him]
    gp, lam, n_rot, n_sweeps, deg = _proj(x, w, n, r, conn, pu)
    total = 0.0
    for j in range(r):
        for i in range(n):
            total += gp[j][i][0] + gp[j][i][1]
    return {
        "kernel": "ltbf_proj", "n": n, "r": r, "conn": conn, "rank": rank, "n_rot": n_rot,
        "n_sweeps": n_sweeps, "degenerate": deg, "gp_sum_bits": _bits(total), "lam_bits": _bits(lam),
    }  # fmt: skip


def proj_counters(wsc: np.ndarray, n: int, pu: int, r: int) -> dict[str, float]:
    """The cost model's counters for one real UE: *wsc* its columns of ``2^(s+m)·W`` (``8·conn x
    8``), ``n = N_rx``, *pu* its row block; the Jacobi work of its own ``M``."""
    w = [[[float(v.real), float(v.imag)] for v in row] for row in wsc]
    m = _m_block(w, pu)
    _v, n_rot, n_sweeps = _jacobi(m)
    return {"n": float(n), "r": float(r), "conn": float(wsc.shape[0] // K),
            "n_rot": float(n_rot), "n_sweeps": float(n_sweeps)}  # fmt: skip


def _features(row: dict) -> dict:
    n, r, conn = float(row["n"]), float(row["r"]), float(row["conn"])
    return {"n": n, "nr": n * r, "conn": conn, "conn_r": conn * r,
            "n_rot": float(row["n_rot"]), "n_sweeps": float(row["n_sweeps"])}  # fmt: skip


KERNEL = Kernel(
    name="ltbf_proj",
    twin=ltbf_proj,
    args=("n", "r", "conn", "rank", "seed"),
    counters=("n", "r", "conn", "n_rot", "n_sweeps"),
    working_set=lambda o: 16.0
    * K
    * (o["n"] + K * o["conn"]),  # X and W, complex doubles
    smoke=(
        {"n": 8, "r": 1, "conn": 2, "rank": 1, "seed": 1},
        {"n": 40, "r": 2, "conn": 9, "rank": 8, "seed": 7},
    ),
    symbols=("ltbf_proj", "jacobi"),
    source_path=SOURCE,
)

FAMILY = Family(
    "ltbf_proj", "ltbf_proj", ("n", "r", "conn", "n_rot", "n_sweeps"),
    ("n", "nr", "conn", "conn_r", "n_rot", "n_sweeps"), transform=_features,
)  # fmt: skip

CAL = a53cal.Calibration(KERNEL, FAMILY, SOURCE, PLAN, DATA, "ltbf_proj_rows")

if __name__ == "__main__":
    sys.exit(CAL.main())
