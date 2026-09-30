"""detectors.py — linear MIMO detectors and the CG solves behind CG-MMSE.

Step 1.2 of ``plans/mimo_cg/mimo_cg_paper_sims.md``.  Every function works on batches: a leading
batch shape ``...`` of independent channel realizations, with ``H`` of shape ``(..., M, K)`` and a
block of received vectors ``Y`` of shape ``(..., M, Ns)``.

CG variants
-----------
* :func:`cg_vector` — textbook CG for a single right-hand side, the independent reference.
* :func:`cg_multi_rhs` — CG run on every column of ``B`` at once.  The columns share the matrix
  product ``S = A P`` (the systolic-array operation) while each keeps its own step size α and
  update coefficient β.  This is *not* O'Leary's block CG, whose steps are matrices.  ``B = HᴴY``
  detects a block directly and ``B = I`` builds the inverse.  ``explicit_residual=True``
  recomputes ``R = B − A X`` (a second matrix product per iteration) instead of the recurrence
  ``R ← R − α S``.  A column whose residual reaches exactly zero freezes: α = 0 when
  ``pᴴ A p = 0`` and β = 0 when the previous ``rᴴ r = 0``, the guard the fixed-point version needs.

Unbiasing
---------
MMSE and CG estimates are biased, ``E[x̂_k | x_k] = μ_k x_k``.  Before hard QAM slicing, each stream is
divided by the exact ``μ_k = 1 − σ²[A⁻¹]_kk`` (:func:`mmse_bias`), the same scalar for exact
MMSE and every CG iterate (plan §14).  A BER gap between them is then purely solver error.

Theory
------
Under ZF in i.i.d. Rayleigh fading, stream ``k``'s post-detection SNR is ρ·g with
``g = 1/[(HᴴH)⁻¹]_kk ~ Gamma(M − K + 1, 1)``.  That is the SNR of maximum-ratio combining over
``L = M − K + 1`` branches, so ZF with QPSK has the MRC closed form (:func:`ber_zf_qpsk_rayleigh`).
For larger QAM, :func:`ber_zf_rayleigh` integrates the exact AWGN BER over that Gamma law.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable

import numpy as np

from examples.mimo_cg.mimo_link import ber_qam_awgn


def _herm(x: np.ndarray) -> np.ndarray:
    """Conjugate transpose of the last two axes."""
    return np.conj(np.swapaxes(x, -1, -2))


def gram(H: np.ndarray) -> np.ndarray:
    """``G = HᴴH``, shape ``(..., K, K)``."""
    return _herm(H) @ H


def mmse_matrix(H: np.ndarray, sigma2: float) -> np.ndarray:
    """``A = HᴴH + σ²I``, the Hermitian positive-definite system CG solves."""
    G = gram(H)
    return G + sigma2 * np.eye(G.shape[-1])


def zf(H: np.ndarray, Y: np.ndarray) -> np.ndarray:
    """Zero-forcing estimates ``(HᴴH)⁻¹HᴴY`` (unbiased)."""
    return np.linalg.solve(gram(H), _herm(H) @ Y)


def mmse(H: np.ndarray, Y: np.ndarray, sigma2: float) -> np.ndarray:
    """Exact MMSE estimates ``A⁻¹HᴴY`` (biased; see :func:`mmse_bias`)."""
    return np.linalg.solve(mmse_matrix(H, sigma2), _herm(H) @ Y)


def bias_from_system(A: np.ndarray, sigma2: float) -> np.ndarray:
    """MMSE bias ``μ_k = 1 − σ²[A⁻¹]_kk`` from an already-formed ``A = HᴴH + σ²I``.

    Shape ``(..., K, 1)``, so an estimate block ``(..., K, Ns)`` divides by it directly.
    """
    inv_diag = np.real(np.diagonal(np.linalg.inv(A), axis1=-2, axis2=-1))
    return (1.0 - sigma2 * inv_diag)[..., :, None]


def mmse_bias(H: np.ndarray, sigma2: float) -> np.ndarray:
    """Per-stream MMSE bias ``μ_k = 1 − σ²[A⁻¹]_kk = [A⁻¹G]_kk``, shape ``(..., K, 1)``."""
    return bias_from_system(mmse_matrix(H, sigma2), sigma2)


def _column_dot(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Real part of the per-column inner products ``uᴴv``, shape ``(..., 1, N)``."""
    return np.real(np.sum(np.conj(u) * v, axis=-2, keepdims=True))


def _guarded_div(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """``num / den``, with 0 wherever ``den == 0`` (a converged column freezes)."""
    out = np.zeros(np.broadcast_shapes(num.shape, den.shape))
    np.divide(num, den, out=out, where=den != 0)
    return out


def cg_multi_rhs(
    A: np.ndarray,
    B: np.ndarray,
    nit: int,
    *,
    explicit_residual: bool = False,
    jacobi: bool = False,
    iterates: Iterable[int] | None = None,
    on_iteration: Callable[[int, dict[str, np.ndarray]], None] | None = None,
) -> np.ndarray | dict[int, np.ndarray]:
    """Run CG on every column of ``A X = B`` for ``nit`` iterations, starting from ``X = 0``.

    Parameters
    ----------
    A : ndarray, shape ``(..., K, K)``
        Hermitian positive definite.
    B : ndarray, shape ``(..., K, N)``
        Right-hand sides, one per column.
    nit : int
        Iteration count.
    explicit_residual : bool, optional
        Recompute ``R = B − A X`` each iteration instead of the recurrence ``R ← R − α S``.
    jacobi : bool, optional
        Precondition with ``diag(A)⁻¹`` (PCG).  Off by default.
    iterates : iterable of int, optional
        If given, return ``{n: X_n}`` for each ``n`` in it (each ``0 <= n <= nit``) instead of
        ``X_nit``.  CG's iterates are a prefix of one run, so this costs one run.
    on_iteration : callable, optional
        Called as ``on_iteration(n, values)`` after setup (``n = 0``: ``X``, ``R``, ``P``, ``rz``)
        and after each iteration ``n >= 1`` with that iteration's ``P`` (the direction it used),
        ``S``, ``ps``, ``alpha``, ``X``, ``R``, ``rz`` and ``beta``.  ``rz`` is ``rᴴr`` (``rᴴz``
        with Jacobi).  Used by :func:`profile_ranges`; the arrays must not be modified.

    Returns
    -------
    ndarray or dict
        ``X_nit``, shape ``np.broadcast_shapes(A.shape[:-2], B.shape[:-2]) + (K, N)``; or the dict.
    """
    wanted = None if iterates is None else set(iterates)
    if wanted is not None and any(n < 0 or n > nit for n in wanted):
        raise ValueError(f"iterates must lie in [0, {nit}], got {sorted(wanted)}")
    B = np.asarray(B, dtype=complex)
    shape = np.broadcast_shapes(A.shape[:-2], B.shape[:-2]) + B.shape[-2:]
    X = np.zeros(shape, dtype=complex)
    R = np.broadcast_to(B, shape).copy()
    dinv = (
        1.0 / np.real(np.diagonal(A, axis1=-2, axis2=-1))[..., :, None]
        if jacobi
        else None
    )
    Z = R * dinv if jacobi else R
    P = Z.copy()
    rz = _column_dot(R, Z)
    out: dict[int, np.ndarray] = {}
    if wanted is not None and 0 in wanted:
        out[0] = X.copy()
    if on_iteration is not None:
        on_iteration(0, {"X": X, "R": R, "P": P, "rz": rz})
    for n in range(1, nit + 1):
        S = A @ P
        ps = _column_dot(P, S)
        alpha = _guarded_div(rz, ps)
        X = X + P * alpha
        R = B - A @ X if explicit_residual else R - S * alpha
        Z = R * dinv if jacobi else R
        rz_new = _column_dot(R, Z)
        beta = _guarded_div(rz_new, rz)
        rz = rz_new
        if on_iteration is not None:
            on_iteration(
                n,
                {
                    "P": P,
                    "S": S,
                    "ps": ps,
                    "alpha": alpha,
                    "X": X,
                    "R": R,
                    "rz": rz,
                    "beta": beta,
                },
            )
        P = Z + P * beta
        if wanted is not None and n in wanted:
            out[n] = X.copy()
    return out if wanted is not None else X


#: The CG variables :func:`profile_ranges` reports, in table order.
RANGE_VARIABLES = ("A", "B", "P", "S", "ps", "alpha", "X", "R", "rz", "beta")


def _max_component(v: np.ndarray) -> float:
    """``max(|Re|, |Im|)`` over every entry: what a complex fixed-point format must hold."""
    v = np.asarray(v)
    return float(max(np.max(np.abs(v.real)), np.max(np.abs(v.imag)))) if v.size else 0.0


def profile_ranges(
    A: np.ndarray,
    B: np.ndarray,
    nit: int,
    *,
    scale: float = 1.0,
    explicit_residual: bool = False,
) -> dict[str, np.ndarray]:
    """Dynamic range of every CG variable, per iteration, for choosing fixed-point formats.

    Runs :func:`cg_multi_rhs` on ``(A / scale) X = B / scale``.  The solution is unchanged, and
    ``scale = M`` is the normalization that keeps ``HᴴH/M`` near the identity.

    Returns
    -------
    dict
        For each name in :data:`RANGE_VARIABLES`, an array of shape ``(nit + 1,)``: the maximum
        over the batch and all entries of ``max(|Re|, |Im|)`` at iteration ``n`` (NaN where the
        variable is undefined, e.g. ``S`` at ``n = 0``).  ``A`` and ``B`` are constant.  Also
        ``"ps_min"`` and ``"rz_min"``: the smallest divisor of α and β at each iteration, which
        sets the range the fixed-point division must handle.
    """
    A = np.asarray(A) / scale
    B = np.asarray(B, dtype=complex) / scale
    table = {
        name: np.full(nit + 1, np.nan)
        for name in RANGE_VARIABLES + ("ps_min", "rz_min")
    }
    table["A"][:] = _max_component(A)
    table["B"][:] = _max_component(B)

    def record(n: int, values: dict[str, np.ndarray]) -> None:
        for name, v in values.items():
            table[name][n] = _max_component(v)
        if "ps" in values:
            table["ps_min"][n] = float(np.min(np.abs(values["ps"])))
        table["rz_min"][n] = float(np.min(np.abs(values["rz"])))

    cg_multi_rhs(A, B, nit, explicit_residual=explicit_residual, on_iteration=record)
    return table


def cg_vector(A: np.ndarray, b: np.ndarray, nit: int) -> np.ndarray:
    """Textbook CG on one system ``A x = b`` (``A``: ``(K, K)``, ``b``: ``(K,)``), from ``x = 0``."""
    x = np.zeros(b.shape, dtype=complex)
    r = np.asarray(b, dtype=complex).copy()
    p = r.copy()
    rr = np.vdot(r, r).real
    for _ in range(nit):
        s = A @ p
        ps = np.vdot(p, s).real
        a = rr / ps if ps != 0 else 0.0
        x = x + a * p
        r = r - a * s
        rr_new = np.vdot(r, r).real
        p = r + (rr_new / rr if rr != 0 else 0.0) * p
        rr = rr_new
    return x


def ber_mrc(gamma_b: np.ndarray | float, L: int) -> np.ndarray:
    """Closed-form BPSK/QPSK BER of MRC over ``L`` i.i.d. Rayleigh branches.

    ``gamma_b`` is the mean per-bit SNR of one branch (Proakis, *Digital Communications*,
    MRC with ``L``-fold diversity).
    """
    g = np.asarray(gamma_b, dtype=float)
    mu = np.sqrt(g / (1.0 + g))
    p, q = (1.0 - mu) / 2.0, (1.0 + mu) / 2.0
    return p**L * sum(math.comb(L - 1 + k, k) * q**k for k in range(L))


def ber_zf_qpsk_rayleigh(rho_db: np.ndarray | float, M: int, K: int) -> np.ndarray:
    """ZF-QPSK BER in i.i.d. Rayleigh: MRC with ``M − K + 1`` branches at per-bit SNR ρ/2."""
    return ber_mrc(10.0 ** (np.asarray(rho_db, dtype=float) / 10.0) / 2.0, M - K + 1)


def ber_zf_rayleigh(
    rho_db: float, M: int, K: int, order: int, n_grid: int = 20001
) -> float:
    """ZF BER of Gray square QAM in i.i.d. Rayleigh, integrating the exact AWGN BER over
    ``g ~ Gamma(M − K + 1, 1)`` (trapezoid rule over ±12 standard deviations)."""
    L = M - K + 1
    lo = max(1e-9, L - 12.0 * math.sqrt(L) - 12.0)
    hi = L + 12.0 * math.sqrt(L) + 12.0
    g = np.linspace(lo, hi, n_grid)
    pdf = np.exp((L - 1) * np.log(g) - g - math.lgamma(L))
    rho = 10.0 ** (rho_db / 10.0)
    return float(np.trapezoid(ber_qam_awgn(rho * g, order) * pdf, g))
