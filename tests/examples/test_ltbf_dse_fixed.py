"""examples/ltbf_dse/ltbf_fixed_chain.py (plans/ltbf_dse/ltbf_dse.md step 3.1, AC6): the fixed-point
chain is what the engines compute.

Each engine's arithmetic in the chain is checked, job by job on random problems, against that
engine's own bit-exact model: the systolic jobs against :func:`waveflow.linalg.matmul.matmul`, the CG
step and the equalizer against :func:`waveflow.vecunit.model.run_job` running the actual programs
(``hw/programs.py``).
"""

from __future__ import annotations

import numpy as np
import pytest

from examples.ltbf_dse import ltbf_fixed as lf
from examples.ltbf_dse import ltbf_fixed_chain as fc
from examples.ltbf_dse.hw import programs as pg
from waveflow.linalg import cg as lcg
from waveflow.linalg import matmul as mmod
from waveflow.vecunit.model import run_job

#: A profile like drop 1's at 8x8 (ltbf_fixed_chain.profile), for formats that exercise every shift.
RANGES = {"H": 0.025, "G": 0.24, "BC": 0.08, "X": 6.8, "Y": 6.7, "Z": 8.5, "R": 0.07, "S": 0.02,
          "ps": 0.05, "rz": 0.18, "alpha": 200.0, "beta": 5.1, "w": 0.5, "y": 1.8, "xh": 1.8}  # fmt: skip


def _ints(rng, shape, f):
    lo, hi = -(1 << (f.W - 1)), (1 << (f.W - 1))
    return rng.integers(lo, hi, shape), rng.integers(lo, hi, shape)


@pytest.mark.parametrize("seed", range(4))
def test_systolic_jobs_equal_the_matmul_model(seed):
    """Every systolic job shape of the chain (T, U, W, Z: ``Aᴴ·B`` and ``A·B``; K explicit),
    ``ltbf_fixed.mm`` (float64 BLAS) equal to ``linalg.matmul.matmul`` -- 5 jobs a seed, 20 in all.
    """
    rng = np.random.default_rng(seed)
    cfg = fc.formats(RANGES, 12 + 2 * seed)
    n, cols, c, r = 32, 24, 8, 2
    a, bc = cfg.a, cfg.bc
    jobs = [
        (_ints(rng, (n, cols), a), a, _ints(rng, (n, c), bc), bc, True),  # T = Ĥα'ᴴD
        (_ints(rng, (n, cols), a), a, _ints(rng, (cols, c), bc), bc, False),  # U = Ĥα'T
        (_ints(rng, (n, cols), a), a, _ints(rng, (n, c), bc), bc, True),  # W = Ĥα'ᴴX_b
        (_ints(rng, (n, r), a), a, _ints(rng, (n, 40), bc), bc, True),  # Z = G'Y
        (_ints(rng, (n, n), bc), bc, _ints(rng, (n, c), bc), bc, False),  # U = K'D
    ]
    for (ar, ai), fa, (br, bi), fb, adj in jobs:
        got = lf.mm(ar, ai, fa, br, bi, fb, bc, adjoint=adj)
        want = mmod.matmul(ar, ai, fa, br, bi, fb, bc, adjoint=adj)
        assert np.array_equal(got[0], want[0]) and np.array_equal(got[1], want[1])


def _state(rng, f: lcg.CgFormats, k, n, zero_col):
    xr, xi = _ints(rng, (k, n), f.X)
    rr, ri = (
        v >> 3 for v in _ints(rng, (k, n), f.R)
    )  # keep |R|^2 sums inside rz's range
    dr, di = (v >> 3 for v in _ints(rng, (k, n), f.P))
    if zero_col:
        rr[:, 0] = ri[:, 0] = dr[:, 0] = di[:, 0] = 0  # ps = rz = 0: the zero divisor
    rz_full, rz_fmt = lcg._column_dot(rr, ri, f.R, rr, ri, f.R)
    return lcg.CgState(xr, xi, rr, ri, dr, di, lcg._q(rz_full, rz_fmt, f.rz))


@pytest.mark.parametrize("case", range(20))
def test_cg_step_equals_the_vector_unit_program(case):
    """The chain's CG step (``S = q_S(U + 2^-2s D)`` then ``linalg.cg.vec_step``, and the last
    iteration's ``X_b = q_bc(2^-m X)``) equals ``run_job`` of ``hw/programs.cg_step_programs`` on
    stored integers: 20 problems, scales ``s`` 0 and 2, two sizes, one with a zero divisor.
    """
    rng = np.random.default_rng(100 + case)
    cfg = fc.formats(RANGES, (12, 14, 16)[case % 3])
    s, (k, n) = (0, 2)[case % 2], ((16, 8), (32, 16))[case % 4 // 2]
    st = _state(rng, cfg.cg, k, n, zero_col=case == 7)
    ur, ui = (v >> 2 for v in _ints(rng, (k, n), cfg.bc))
    last = case % 5 == 0
    f = cfg.cg_step()
    vu = pg.cg_step_config(
        f, k, n, L=8, lane_bits=cfg.lane_bits, m_max=max(8, cfg.m_shift)
    )
    progs = pg.cg_step_programs(vu, k, n, s, last_m=cfg.m_shift if last else None)
    res = run_job(
        vu,
        *progs,
        {pg.PORT: [(st.xr, st.xi), (st.rr, st.ri), (st.pr, st.pi), (ur, ui)]},
    )
    assert int(res.status) == 0
    got = {resp["tag"]: (re, im) for resp, re, im in res.outputs[pg.PORT]}
    sr, si = fc.cg_step_s(st.pr, st.pi, ur, ui, cfg, s)
    new, _ = lcg.vec_step(st, sr, si, cfg.cg)
    want = {pg.OX: (new.xr, new.xi), pg.OR: (new.rr, new.ri), pg.OD: (new.pr, new.pi)}
    if last:
        xb = fc.Format(cfg.cg.X.W + cfg.m_shift, cfg.cg.X.int_bits, True)
        want[pg.OXB] = (
            fc.fx.quantize(new.xr, xb, cfg.bc),
            fc.fx.quantize(new.xi, xb, cfg.bc),
        )
    assert set(got) == set(want)
    for tag, (re, im) in want.items():
        assert np.array_equal(got[tag][0], re) and np.array_equal(got[tag][1], im), tag


@pytest.mark.parametrize("case", range(20))
def test_equalizer_equals_the_vector_unit_program(case):
    """``equalize_fixed`` equals ``run_job`` of ``hw/programs.eq_programs`` on random tiles (one
    weight an RE): 20 tiles, r = 1 and 2, three widths."""
    rng = np.random.default_rng(200 + case)
    cfg = fc.formats(RANGES, (12, 14, 16)[case % 3])
    r, u_pad, c = 1 + case % 2, 16, 8
    z = [_ints(rng, (u_pad, c), cfg.eq.z) for _ in range(r)]
    w = [(v >> 2 for v in _ints(rng, (u_pad, c), cfg.eq.w)) for _ in range(r)]
    w = [tuple(x) for x in w]
    vu = pg.eq_config(cfg.eq, u_pad, c, L=8, r=r, lane_bits=cfg.lane_bits)
    ins = []
    for k in range(r):
        ins += [w[k], z[k]]
    res = run_job(vu, *pg.eq_programs(vu, u_pad, c, r, tiles=1), {pg.PORT: ins})
    assert int(res.status) == 0
    _resp, gr, gi = res.outputs[pg.PORT][0]
    zr, zi = np.stack([x[0] for x in z]), np.stack([x[1] for x in z])
    wr, wi = np.stack([x[0] for x in w]), np.stack([x[1] for x in w])
    xr, xi = fc.equalize_fixed(zr, zi, wr, wi, cfg.eq)
    assert np.array_equal(gr, xr) and np.array_equal(gi, xi)


@pytest.mark.parametrize("case", range(6))
def test_one_unit_runs_both_programs(case):
    """``hw/programs.system_config``: one vector-unit build (the CG step's registers and queues, then
    the equalizer's) runs the CG step's last-iteration program and the equalizer's (with its
    ``Offsets``) bit for bit as their own builds do; ``k = N_rx = 64``, ``U_pad = 16``.
    """
    rng = np.random.default_rng(300 + case)
    cfg = fc.formats(RANGES, (12, 14, 16)[case % 3])
    k, n, u_pad, r, s = 64, 8, 16, 1 + case % 2, 1
    vu, off = pg.system_config(cfg.cg_step(), cfg.eq, k=k, n=n, u_pad=u_pad, r=r, L=8,
                               lane_bits=cfg.lane_bits, m_max=max(8, cfg.m_shift))  # fmt: skip
    st = _state(rng, cfg.cg, k, n, zero_col=case == 3)
    ur, ui = (v >> 2 for v in _ints(rng, (k, n), cfg.bc))
    res = run_job(vu, *pg.cg_step_programs(vu, k, n, s, last_m=cfg.m_shift),
                  {pg.PORT: [(st.xr, st.xi), (st.rr, st.ri), (st.pr, st.pi), (ur, ui)]})  # fmt: skip
    assert int(res.status) == 0
    got = {resp["tag"]: (re, im) for resp, re, im in res.outputs[pg.PORT]}
    sr, si = fc.cg_step_s(st.pr, st.pi, ur, ui, cfg, s)
    new, _ = lcg.vec_step(st, sr, si, cfg.cg)
    xb = fc.Format(cfg.cg.X.W + cfg.m_shift, cfg.cg.X.int_bits, True)
    want = {pg.OX: (new.xr, new.xi), pg.OR: (new.rr, new.ri), pg.OD: (new.pr, new.pi),
            pg.OXB: (fc.fx.quantize(new.xr, xb, cfg.bc), fc.fx.quantize(new.xi, xb, cfg.bc))}  # fmt: skip
    for tag, (re, im) in want.items():
        assert np.array_equal(got[tag][0], re) and np.array_equal(got[tag][1], im), tag
    z = [_ints(rng, (u_pad, n), cfg.eq.z) for _ in range(r)]
    w = [tuple(v >> 2 for v in _ints(rng, (u_pad, n), cfg.eq.w)) for _ in range(r)]
    ins = [x for q in range(r) for x in (w[q], z[q])]
    res = run_job(
        vu, *pg.eq_programs(vu, u_pad, n, r, tiles=2, off=off), {pg.PORT: ins + ins}
    )
    assert int(res.status) == 0
    zr, zi = np.stack([x[0] for x in z]), np.stack([x[1] for x in z])
    wr, wi = np.stack([x[0] for x in w]), np.stack([x[1] for x in w])
    xr, xi = fc.equalize_fixed(zr, zi, wr, wi, cfg.eq)
    assert len(res.outputs[pg.PORT]) == 2
    for _resp, gr, gi in res.outputs[pg.PORT]:
        assert np.array_equal(gr, xr) and np.array_equal(gi, xi)
