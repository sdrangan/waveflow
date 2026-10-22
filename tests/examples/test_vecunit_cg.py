"""CG as a program (Stage 3b of ``plans/vecunit_new.md``): the interpreter, with the harness playing the
systolic array (``S = mm_step(A, P)`` on each ``P`` the unit emits), equals ``cg.cg_solve`` bit for
bit -- every ``P`` it emits and the final ``X`` -- for random Hermitian positive definite ``A``, the
``mimo_cg`` study's formats, ``nit`` 1..8, ``k`` and ``n`` at and below the maxima, and the zero
guards (a column that freezes, ``ps = 0``, ``rz = 0``)."""

from __future__ import annotations

import numpy as np
import pytest

from examples.vecunit_cg.cg_program import cg_config, cg_programs, run_cg
from waveflow.linalg import cg
from waveflow.utils import fixputils as fxu
from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit.asm import check

#: Integer bits per register, sign included: the mimo_cg study's parameterization (its INT_BITS).
INT_BITS = {"A": 3, "B": 4, "P": 4, "R": 4, "S": 5, "X": 3, "ps": 10, "rz": 9, "alpha": 5, "beta": 3}


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


def study_formats(W: int, g: int = 0, g_div: int = 6) -> cg.CgFormats:
    """``W``-bit vectors, ``alpha`` and ``beta``; ``W + g``-bit ``ps`` and ``rz`` (``W12g8`` is
    ``study_formats(12, 8)``)."""
    regs = {v: reg(W, INT_BITS[v]) for v in ("A", "B", "P", "R", "S", "X", "alpha", "beta")}
    regs |= {s: reg(W + g, INT_BITS[s]) for s in ("ps", "rz")}
    return cg.CgFormats(**regs, g_div=g_div)


def stress_formats() -> cg.CgFormats:
    """The study's saturation stress set: ``alpha`` and ``beta`` with 2 and 1 integer bits."""
    regs = {v: reg(12, INT_BITS[v]) for v in ("A", "B", "P", "R", "S", "X")}
    regs |= {"ps": reg(14, 10), "rz": reg(14, 9), "alpha": reg(14, 2), "beta": reg(14, 1)}
    return cg.CgFormats(**regs, g_div=2)


FORMATS = {"W12g8": study_formats(12, 8), "W10g0": study_formats(10, 0, 4),
           "W16g4": study_formats(16, 4), "stress": stress_formats()}


def problem(f: cg.CgFormats, k: int, n: int, seed: int):
    """A random Hermitian positive definite ``A`` (``k x k``) and ``B`` (``k x n``), quantized."""
    rng = np.random.default_rng(seed)
    m = 4 * k
    H = (rng.standard_normal((m, k)) + 1j * rng.standard_normal((m, k))) / np.sqrt(2)
    Y = (rng.standard_normal((m, n)) + 1j * rng.standard_normal((m, n))) / np.sqrt(2)
    A = (H.conj().T @ H + 0.1 * np.eye(k)) / m
    B = H.conj().T @ Y / m
    q = fxu.quantize_real
    return q(A.real, f.A), q(A.imag, f.A), q(B.real, f.B), q(B.imag, f.B)


def golden(f, ar, ai, br, bi, nit):
    """``P0 .. P(nit-1)`` and ``X``, and every scalar, from the model."""
    ps_seen, rz_seen = [], []
    state = cg.cg_init(br, bi, f)
    ps_list = [(state.pr, state.pi)]
    rz_seen.append(state.rz)
    for it in range(1, nit + 1):
        sr, si = cg.mm_step(ar, ai, state.pr, state.pi, f)
        state, sc = cg.vec_step(state, sr, si, f)
        ps_seen.append(sc["ps"])
        rz_seen.append(state.rz)
        if it < nit:
            ps_list.append((state.pr, state.pi))
    x = cg.cg_solve(ar, ai, br, bi, nit, f)
    assert np.array_equal(x.xr, state.xr)
    return ps_list, (state.xr, state.xi), ps_seen, rz_seen


def assert_matches(f, ar, ai, br, bi, nit, *, mode="serial", skip_dead=True):
    k, n = br.shape
    cfg = cg_config(f, Kmax=8, Nmax=32, L=4)
    assert check(cfg, cg_programs(cfg, nit, k, n, skip_dead=skip_dead)) == []
    r = run_cg(cfg, f, ar, ai, br, bi, nit, tx_id=7, mode=mode, skip_dead=skip_dead)
    assert r.status == 0
    p_gold, (xr, xi), ps_seen, rz_seen = golden(f, ar, ai, br, bi, nit)
    outs = r.outputs[0]
    assert len(outs) == nit + 1
    for it, ((pr, pi), (resp, ur, ui)) in enumerate(zip(p_gold, outs[:-1])):
        assert resp["tag"] == it and np.array_equal(ur, pr) and np.array_equal(ui, pi), f"P{it}"
    assert np.array_equal(outs[-1][1], xr) and np.array_equal(outs[-1][2], xi), "X"
    return ps_seen, rz_seen


@pytest.mark.parametrize("fname", list(FORMATS))
@pytest.mark.parametrize("nit", [1, 2, 3, 5, 8])
@pytest.mark.parametrize("k, n", [(8, 32), (8, 13), (5, 4), (3, 32), (1, 1)])
def test_cg_program_equals_cg_solve(fname, nit, k, n):
    f = FORMATS[fname]
    assert_matches(f, *problem(f, k, n, seed=nit * 31 + k * 7 + n), nit)


@pytest.mark.parametrize("fname", ["W12g8", "stress"])
def test_element_major_passes_and_the_full_last_iteration(fname):
    """Element-major passes (the hardware's order) and the program that keeps the last iteration's
    dead work give the same bits."""
    f = FORMATS[fname]
    prob = problem(f, 8, 12, seed=5)
    assert_matches(f, *prob, 3, mode="pass")
    assert_matches(f, *prob, 3, skip_dead=False)


def test_zero_guards():
    """A zero column of ``B``: its ``R``, ``P`` and so ``ps`` and ``rz`` are zero from the start, so
    both guards fire every iteration and the column stays ``X = 0``.  A converged column (``A = B =
    1``, ``k = n = 1``) freezes after one iteration (the guards again)."""
    f = FORMATS["W12g8"]
    ar, ai, br, bi = problem(f, 8, 9, seed=11)
    br[:, 3] = bi[:, 3] = 0
    ps_seen, rz_seen = assert_matches(f, ar, ai, br, bi, 4)
    assert all((ps[0, 3] == 0) for ps in ps_seen) and all(rz[0, 3] == 0 for rz in rz_seen)
    one_a = np.array([[1 << f.A.frac_bits]]), np.zeros((1, 1), np.int64)
    one_b = np.array([[1 << f.B.frac_bits]]), np.zeros((1, 1), np.int64)
    ps_seen, rz_seen = assert_matches(f, one_a[0], one_a[1], one_b[0], one_b[1], 3)
    assert ps_seen[1][0, 0] == 0 and rz_seen[1][0, 0] == 0  # frozen after iteration 1


def test_the_program_counts():
    f = FORMATS["W12g8"]
    cfg = cg_config(f)
    p = cg_programs(cfg, 3, 8, 32)
    # start: setvl, 8 X = 0, 8 x 3, qpop, 8 vsse, qpush; per iteration 8 x 9 + 2 (the last 8 x 5 + 2)
    assert len(p.execute) == 1 + 8 + 24 + 1 + 8 + 1 + 2 * (8 * 9 + 2) + (8 * 5 + 2)
    assert len(p.load) == 4 and len(p.store) == 4
