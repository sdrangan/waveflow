"""AC1.2 of plans/mimo_cg/mimo_cg_paper_sims.md: detectors and CG against theory and exact solves.

Seeds are fixed here and must never be changed to make a test pass (the plan's Rules 4).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from examples.mimo_cg.detectors import (
    ber_zf_qpsk_rayleigh,
    ber_zf_rayleigh,
    cg_multi_rhs,
    cg_vector,
    gram,
    mmse,
    mmse_bias,
    mmse_matrix,
    zf,
)
from examples.mimo_cg.mimo_link import (
    BerCount,
    Qam,
    noise_variance,
    point_rng,
    rayleigh,
    snr_key,
)

MK = [(M, K) for M in (32, 64, 128) for K in (4, 8, 16)]
SEEDS = range(10)
SIGMA2 = 0.1  # rho = 10 dB
NS = 32  # received vectors per block, as in the BER runs


def _system(M: int, K: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = point_rng(10, M, K, seed)
    H = rayleigh(rng, (M, K))
    Y = rayleigh(rng, (M, NS))
    return H, mmse_matrix(H, SIGMA2), H.conj().T @ Y


def _a_norm(A: np.ndarray, e: np.ndarray) -> np.ndarray:
    """Per-column A-norm ``sqrt(eᴴ A e)``."""
    return np.sqrt(np.real(np.sum(np.conj(e) * (A @ e), axis=0)))


def _rel(x: np.ndarray, ref: np.ndarray) -> float:
    return float(np.linalg.norm(x - ref) / np.linalg.norm(ref))


# (a) ZF against the MRC closed form -------------------------------------------------------

#: (rho in dB, closed-form BER computed at planning) for M = 32, K = 16, QPSK.
ZF_POINTS = [(-10, 1.005e-1), (-5, 1.328e-2)]
ZF_REALIZATIONS = 50_000  # independent channels, one received vector each
ZF_CHUNK = 10_000


@pytest.mark.parametrize("rho_db,planned", ZF_POINTS)
def test_zf_qpsk_ber_lies_inside_the_999_interval_of_the_mrc_closed_form(
    rho_db, planned
):
    M, K = 32, 16
    theory = float(ber_zf_qpsk_rayleigh(rho_db, M, K))
    assert theory == pytest.approx(planned, rel=2e-3)
    qam = Qam(4)
    rng = point_rng(11, M, K, snr_key(rho_db))
    sigma = math.sqrt(noise_variance(rho_db))
    count = BerCount()
    for _ in range(ZF_REALIZATIONS // ZF_CHUNK):
        H = rayleigh(rng, (ZF_CHUNK, M, K))
        bits = rng.integers(0, 2, size=(ZF_CHUNK, K * qam.bits_per_symbol))
        y = H @ qam.modulate(bits)[..., None] + sigma * rayleigh(rng, (ZF_CHUNK, M, 1))
        errors = np.count_nonzero(qam.demodulate(zf(H, y)[..., 0]) != bits)
        count.add(errors, bits.size, ZF_CHUNK)
    lo, hi = count.interval(0.999)
    assert (
        lo <= theory <= hi
    ), f"BER {count.ber:.4e} in [{lo:.4e}, {hi:.4e}], theory {theory:.4e}"


@pytest.mark.parametrize("M,K", [(32, 16), (64, 8), (128, 4)])
def test_zf_gamma_integral_reproduces_the_mrc_closed_form_for_qpsk(M, K):
    for rho_db in (-15.0, -10.0, -5.0, 0.0):
        closed = float(ber_zf_qpsk_rayleigh(rho_db, M, K))
        if closed < 1e-30:
            continue
        assert ber_zf_rayleigh(rho_db, M, K, 4) == pytest.approx(closed, rel=1e-4)


# (b) CG against the direct solve ----------------------------------------------------------


@pytest.mark.parametrize("M,K", MK)
def test_cg_matches_the_direct_solve_at_nit_equal_k(M, K):
    for seed in SEEDS:
        _, A, B = _system(M, K, seed)
        ref = np.linalg.solve(A, B)
        assert _rel(cg_multi_rhs(A, B, K), ref) <= 1e-8, seed
        eye = np.eye(K, dtype=complex)
        assert _rel(cg_multi_rhs(A, eye, K), np.linalg.inv(A)) <= 1e-8, seed
        assert _rel(cg_vector(A, B[:, 0], K), ref[:, 0]) <= 1e-8, seed
        assert _rel(cg_multi_rhs(A, B, K, jacobi=True), ref) <= 1e-8, seed


# (c) monotone A-norm error ----------------------------------------------------------------


@pytest.mark.parametrize("M,K", MK)
def test_a_norm_error_is_non_increasing(M, K):
    for seed in SEEDS:
        _, A, B = _system(M, K, seed)
        ref = np.linalg.solve(A, B)
        xs = cg_multi_rhs(A, B, K, iterates=range(K + 1))

        scale = _a_norm(A, ref)
        errs = [_a_norm(A, xs[n] - ref) for n in range(K + 1)]
        for n in range(K):
            assert np.all(errs[n + 1] <= errs[n] * (1 + 1e-10) + 1e-14 * scale), (
                seed,
                n,
            )


# (d) recurrence vs explicit residual ------------------------------------------------------


@pytest.mark.parametrize("M,K", MK)
def test_recurrence_and_explicit_residual_forms_agree(M, K):
    for seed in SEEDS:
        _, A, B = _system(M, K, seed)
        rec = cg_multi_rhs(A, B, K)
        exp = cg_multi_rhs(A, B, K, explicit_residual=True)
        assert _rel(rec, exp) <= 1e-8, seed


# (e) multi-RHS columns vs plain CG --------------------------------------------------------


@pytest.mark.parametrize("M,K", MK)
def test_multi_rhs_columns_equal_plain_cg(M, K):
    # Two tiers (plan §14, 2026-09-30).  Far from convergence the two paths agree to rounding
    # (worst 7.2e-15 over this grid); at nit = K both sit at their own convergence error and
    # BLAS gemm-vs-gemv rounding, amplified by CG, separates them by up to 1.6e-10.
    for seed in SEEDS:
        _, A, B = _system(M, K, seed)
        for nit in sorted({1, 2, K // 2, K}):
            tol = 1e-12 if nit <= K // 2 else 1e-9
            X = cg_multi_rhs(A, B, nit)
            for j in range(B.shape[1]):
                gap = _rel(X[:, j], cg_vector(A, B[:, j], nit))
                assert gap <= tol, (seed, nit, j, gap)


# batching and the helpers the BER runs rely on ---------------------------------------------


def test_batched_cg_equals_one_system_at_a_time():
    systems = [_system(64, 8, seed) for seed in range(4)]
    A = np.stack([s[1] for s in systems])
    B = np.stack([s[2] for s in systems])
    batched = cg_multi_rhs(A, B, 5, iterates=[3, 5])
    for i in range(4):
        for n in (3, 5):
            assert _rel(batched[n][i], cg_multi_rhs(A[i], B[i], n)) <= 1e-12


def test_mmse_bias_is_the_diagonal_of_inverse_a_times_gram():
    H, A, _ = _system(32, 16, 0)
    mu = mmse_bias(H, SIGMA2)[:, 0]
    np.testing.assert_allclose(
        mu, np.real(np.diag(np.linalg.solve(A, gram(H)))), rtol=1e-10
    )
    assert np.all((mu > 0) & (mu < 1))


def test_mmse_is_cg_at_convergence_and_zf_is_the_noiseless_limit():
    H, A, _ = _system(64, 8, 1)
    Y = rayleigh(point_rng(10, 64, 8, 1), (64, NS))
    assert _rel(mmse(H, Y, SIGMA2), cg_multi_rhs(A, H.conj().T @ Y, 8)) <= 1e-8
    assert _rel(mmse(H, Y, 1e-12), zf(H, Y)) <= 1e-8
