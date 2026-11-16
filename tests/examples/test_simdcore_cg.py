"""Conjugate gradient on the SIMD core's interpreter: bit for bit against its integer golden, closed
loop through ``S = A P``; its accuracy against float CG and the vector unit's centre build
(``plans/simdcore.md`` Stage 3)."""

from __future__ import annotations

import numpy as np
import pytest

from examples.simdcore_cg.cg_program import accuracy, cg_config, cg_golden, cg_text, problem, run_cg


@pytest.mark.parametrize("K,N,nit,L", [(8, 32, 3, 2), (8, 32, 3, 4), (5, 13, 2, 2), (8, 32, 1, 1),
                                       (3, 64, 4, 8), (8, 32, 6, 2)])
def test_bitexact_vs_golden(K, N, nit, L):
    ar, ai, br, bi = problem(K, N, seed=K * 100 + N)
    res, r = run_cg(cg_config(K, N, L=L), ar, ai, br, bi, nit)
    g = cg_golden(ar, ai, br, bi, nit)
    assert np.array_equal(res.xr, g.xr) and np.array_equal(res.xi, g.xi)
    assert len(r.outputs["OUT"]) == nit + 1  # P0 .. P(nit-1), then X


def test_zero_column_guards():
    """A zero right-hand side: ps = rz = 0, both divide guards give 0, the column stays 0."""
    ar, ai, br, bi = problem(8, 32, seed=1)
    br, bi = br.copy(), bi.copy()
    br[:, 5] = bi[:, 5] = 0
    res, _ = run_cg(cg_config(8, 32), ar, ai, br, bi, 3)
    g = cg_golden(ar, ai, br, bi, 3)
    assert np.array_equal(res.xr, g.xr) and np.array_equal(res.xi, g.xi)
    assert not res.xr[:, 5].any() and not res.xi[:, 5].any()


def test_accuracy_criterion_2():
    """Mean relative residual after 3 iterations, 8 problems: this core's integer CG within 2x of the
    vector unit's (D9); measured 2026-11-07: float 0.0266, this core 0.0282, vector unit 0.0639."""
    a = accuracy(seeds=range(4))
    assert a["simdcore"] <= 2 * a["vecunit_centre"]
    assert a["simdcore"] <= 1.25 * a["float"]


def test_cycles_steady_against_d9():
    """The cycle model at the steady shape (8 x 32, 3 iterations): D9's targets as predictions."""
    ar, ai, br, bi = problem(8, 32, seed=0)
    _, r2 = run_cg(cg_config(8, 32, L=2), ar, ai, br, bi, 3)
    _, r4 = run_cg(cg_config(8, 32, L=4), ar, ai, br, bi, 3)
    assert r2.cycles <= 5170 and r4.cycles <= 3447
    assert "vdiv.vv" in cg_text(8, 3)
