"""cg.py — the bit-exact model of fixed-point conjugate gradient, multi-RHS.

The golden model of the CG vector unit.  It solves ``A X = B`` for Hermitian positive definite
``A`` (``K × K``) and ``N`` right-hand sides ``B`` (``K × N``), every column with its own α and β,
on fixed-point registers.  Every register value is an ``ap_fixed`` stored integer computed with
:mod:`waveflow.utils.fixputils`, and an HLS implementation that keeps every product and sum exact
and rounds where this model rounds is bit-exact with it.

The algorithm
-------------
From ``X = 0``, ``R = B``, ``P = R``, ``rz = Σ|R|²`` (:func:`cg_init`), each iteration performs, in
this order (``q_V`` = quantize to the register format of ``V``, with its own rounding and
saturation; ``w`` = widen the dividend by ``g_div`` fraction bits, which is exact; ``/`` =
:func:`~waveflow.utils.fixputils.div` with its zero guard)::

    1. S     = q_S(A @ P)                     exact sum of complex products over K
    2. ps    = q_ps(Σ_k Re(conj(P) * S))      per column
    3. alpha = q_alpha(w(rz) / ps)            ps == 0 -> alpha = 0 (X unchanged this iteration)
    4. X     = q_X(X + P * alpha)
    5. R     = q_R(R - S * alpha)             the recurrence, or q_R(B - A @ X), the explicit form
    6. rz'   = q_rz(Σ_k |R|²)
    7. beta  = q_beta(w(rz') / rz)            rz == 0 -> beta = 0
    8. rz    = rz'
    9. P     = q_P(R + P * beta)

Step 1 is the matrix multiply (:func:`mm_step`, by :func:`waveflow.linalg.matmul.matmul`); steps
2–9 are the vector unit (:func:`vec_step`).  Every product and sum before a ``q_`` is exact, and
integer arithmetic is associative, so any summation order gives the same register values.  A
column freezes once ``rz == 0``: then α = 0 (zero dividend), β = 0 (the guard), and ``R``, ``P``
stay zero.  A zero ``ps`` with a nonzero ``rz`` only skips that iteration's ``X`` update; β
becomes 1 and CG continues.

The explicit form computes ``R`` from ``B − A X`` instead of the recurrence: :func:`vec_step` takes
it as a ``residual`` hook (:func:`residual_hook`).

Representation
--------------
A complex register is a pair of int64 stored arrays ``(re, im)`` with one
:class:`~waveflow.utils.fixputils.Format`.  Matrices have shape ``(..., K, N)``, the column
scalars ``(..., 1, N)``, and every function is batched over the leading dimensions.  The formats
are plain fields of :class:`CgFormats`; how they are chosen is the caller's.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields

import numpy as np

from waveflow.linalg.matmul import acc_format, matmul, matmul_exact
from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format


@dataclass(frozen=True)
class CgFormats:
    """The register formats of the fixed-point CG, plus the dividend widening ``g_div``."""

    A: Format
    B: Format
    P: Format
    R: Format
    S: Format
    X: Format
    ps: Format
    rz: Format
    alpha: Format
    beta: Format
    g_div: int = 0

    def registers(self) -> dict[str, Format]:
        return {
            f.name: getattr(self, f.name) for f in fields(self) if f.name != "g_div"
        }

    def intermediates(self, K: int) -> dict[str, Format]:
        """Every full-precision intermediate format for ``K`` users.

        Raises ``NotImplementedError`` if any exceeds 64 bits (the :class:`Format` guard).
        """
        f = self
        rz_w = _widened(f.rz, f.g_div)
        out = {
            "A@P": acc_format(f.A, f.P, K),
            "Re(P^H S)": _dot_format(f.P, f.S, K),
            "|R|^2": _dot_format(f.R, f.R, K),
            "rz/ps": fx.div_format(rz_w, f.ps),
            "rz'/rz": fx.div_format(rz_w, f.rz),
            "X+P*alpha": fx.add_format(f.X, fx.mult_format(f.P, f.alpha)),
            "R-S*alpha": fx.sub_format(f.R, fx.mult_format(f.S, f.alpha)),
            "B-A@X": fx.sub_format(f.B, acc_format(f.A, f.X, K)),
            "R+P*beta": fx.add_format(f.R, fx.mult_format(f.P, f.beta)),
        }
        # fixputils.quantize shifts left when the target keeps more fraction bits; that
        # intermediate (source integer bits + target fraction bits) must fit 64 bits too.
        for name, (src, dst) in {
            "q_alpha(rz/ps)": (out["rz/ps"], f.alpha),
            "q_beta(rz'/rz)": (out["rz'/rz"], f.beta),
            "w(rz)": (f.rz, rz_w),
        }.items():
            if src.frac_bits <= dst.frac_bits:
                width = src.int_bits + dst.frac_bits
                if width > 64:
                    raise NotImplementedError(
                        f"{name}: quantize intermediate {width} bits exceeds the 64-bit cap"
                    )
                out[name] = Format(width, src.int_bits, True)
        return out


def accumulator_formats(formats: CgFormats, K: int) -> dict[str, Format]:
    """The exact accumulator formats a hardware or C++ implementation must provide.

    ``mm_ap`` (A @ P), ``mm_ax`` (A @ X, explicit residual), ``dot_ps`` (Σ Re(conj(P) S)),
    ``dot_rz`` (Σ |R|²), and ``rzw``, the widened dividend register.
    """
    f = formats
    return {
        "mm_ap": acc_format(f.A, f.P, K),
        "mm_ax": acc_format(f.A, f.X, K),
        "dot_ps": _dot_format(f.P, f.S, K),
        "dot_rz": _dot_format(f.R, f.R, K),
        "rzw": _widened(f.rz, f.g_div),
    }


def _widened(fmt: Format, g: int) -> Format:
    return Format(fmt.W + g, fmt.int_bits, fmt.signed, fmt.q_mode, fmt.o_mode)


def _dot_format(a: Format, b: Format, K: int) -> Format:
    p = fx.mult_format(a, b)
    return fx.sum_format(fx.add_format(p, p), K)


# --- register-level operations on (re, im) stored-int pairs --------------------------------


def _q(x: np.ndarray, src: Format, dst: Format) -> np.ndarray:
    return fx.quantize(x, src, dst)


def _column_dot(ar, ai, fa: Format, br, bi, fb: Format) -> tuple[np.ndarray, Format]:
    """Exact ``Σ_k Re(conj(a_k) b_k)`` per column, shape ``(..., 1, N)``."""
    K = ar.shape[-2]
    fmt = _dot_format(fa, fb, K)
    return np.sum(ar * br + ai * bi, axis=-2, keepdims=True), fmt


def _scale(ar, ai, fa: Format, s, fs: Format) -> tuple[np.ndarray, np.ndarray, Format]:
    """Exact complex-by-real product (``s`` broadcasts over rows)."""
    fmt = fx.mult_format(fa, fs)
    return ar * s, ai * s, fmt


def _addsub(op, ar, ai, fa: Format, br, bi, fb: Format):
    re, fmt = op(ar, fa, br, fb)
    im, _ = op(ai, fa, bi, fb)
    return re, im, fmt


# --- the state and the steps -----------------------------------------------------------------


@dataclass
class CgState:
    """The vector unit's CG state between iterations, as stored integers.

    ``X``, ``R`` and ``P`` are ``(re, im)`` pairs of shape ``(..., K, N)`` in the register
    formats ``X``, ``R`` and ``P``; ``rz`` has shape ``(..., 1, N)`` in format ``rz``.
    """

    xr: np.ndarray
    xi: np.ndarray
    rr: np.ndarray
    ri: np.ndarray
    pr: np.ndarray
    pi: np.ndarray
    rz: np.ndarray


def cg_init(br: np.ndarray, bi: np.ndarray, formats: CgFormats) -> CgState:
    """The state before iteration 1, from the stored ``B``: ``X = 0``, ``R = q_R(B)``,
    ``P = q_P(R)``, ``rz = q_rz(Σ|R|²)``."""
    f = formats
    rr, ri = _q(br, f.B, f.R), _q(bi, f.B, f.R)
    pr, pi = _q(rr, f.R, f.P), _q(ri, f.R, f.P)
    rz_full, rz_fmt = _column_dot(rr, ri, f.R, rr, ri, f.R)
    zeros = np.zeros(np.shape(br), np.int64)
    return CgState(zeros, zeros.copy(), rr, ri, pr, pi, _q(rz_full, rz_fmt, f.rz))


def mm_step(
    ar: np.ndarray, ai: np.ndarray, pr: np.ndarray, pi: np.ndarray, formats: CgFormats
) -> tuple[np.ndarray, np.ndarray]:
    """Step 1, the matrix multiply: ``S = q_S(A @ P)``, exact before ``q_S``."""
    f = formats
    return matmul(ar, ai, f.A, pr, pi, f.P, f.S)


def vec_step(
    state: CgState,
    sr: np.ndarray,
    si: np.ndarray,
    formats: CgFormats,
    *,
    residual: Callable[[np.ndarray, np.ndarray], tuple] | None = None,
) -> tuple[CgState, dict[str, np.ndarray]]:
    """Steps 2–9, the vector unit: from the state and ``S``, the next state.

    ``residual(xr, xi)`` gives the exact pre-quantize residual ``(re, im, Format)`` of the
    explicit form, ``B − A X`` (:func:`residual_hook`); ``None`` uses the recurrence ``R − S α``.
    Returns the new state, whose ``rz`` is ``rz'``, and ``{"ps", "alpha", "beta"}``.
    """
    f = formats
    rz_w_fmt = _widened(f.rz, f.g_div)
    ps_full, ps_fmt = _column_dot(state.pr, state.pi, f.P, sr, si, f.S)  # 2
    ps = _q(ps_full, ps_fmt, f.ps)
    q, q_fmt = fx.div(_q(state.rz, f.rz, rz_w_fmt), rz_w_fmt, ps, f.ps)  # 3
    alpha = _q(q, q_fmt, f.alpha)
    tr, ti, t_fmt = _scale(state.pr, state.pi, f.P, alpha, f.alpha)  # 4
    xr, xi, x_fmt = _addsub(fx.add, state.xr, state.xi, f.X, tr, ti, t_fmt)
    xr, xi = _q(xr, x_fmt, f.X), _q(xi, x_fmt, f.X)
    if residual is not None:  # 5
        rr, ri, r_fmt = residual(xr, xi)
    else:
        tr, ti, t_fmt = _scale(sr, si, f.S, alpha, f.alpha)
        rr, ri, r_fmt = _addsub(fx.sub, state.rr, state.ri, f.R, tr, ti, t_fmt)
    rr, ri = _q(rr, r_fmt, f.R), _q(ri, r_fmt, f.R)
    rz_full, rz_fmt = _column_dot(rr, ri, f.R, rr, ri, f.R)  # 6
    rz = _q(rz_full, rz_fmt, f.rz)
    q, q_fmt = fx.div(_q(rz, f.rz, rz_w_fmt), rz_w_fmt, state.rz, f.rz)  # 7
    beta = _q(q, q_fmt, f.beta)
    tr, ti, t_fmt = _scale(state.pr, state.pi, f.P, beta, f.beta)  # 9 (8 is rz = rz')
    pr, pi, p_fmt = _addsub(fx.add, rr, ri, f.R, tr, ti, t_fmt)
    pr, pi = _q(pr, p_fmt, f.P), _q(pi, p_fmt, f.P)
    return CgState(xr, xi, rr, ri, pr, pi, rz), {"ps": ps, "alpha": alpha, "beta": beta}


def residual_hook(
    ar: np.ndarray, ai: np.ndarray, br: np.ndarray, bi: np.ndarray, formats: CgFormats
) -> Callable[[np.ndarray, np.ndarray], tuple]:
    """The explicit form's ``residual`` for :func:`vec_step`: the exact ``B − A X``."""
    f = formats

    def residual(xr, xi):
        axr, axi, ax_fmt = matmul_exact(ar, ai, f.A, xr, xi, f.X)
        return _addsub(fx.sub, br, bi, f.B, axr, axi, ax_fmt)

    return residual


def cg_solve(
    ar: np.ndarray,
    ai: np.ndarray,
    br: np.ndarray,
    bi: np.ndarray,
    nit: int,
    formats: CgFormats,
    *,
    explicit: bool = False,
    on_iteration: Callable[[int, CgState, dict], None] | None = None,
) -> CgState:
    """The reference solve on stored integers: :func:`cg_init`, then per iteration
    :func:`mm_step` and :func:`vec_step`; returns the state after ``nit`` iterations.

    ``A`` is ``(..., K, K)`` and ``B`` is ``(..., K, N)``; the leading dimensions broadcast.  The
    formats are checked against the 64-bit cap first.  ``on_iteration(n, state, scalars)`` is
    called after iteration ``n``.
    """
    f = formats
    f.intermediates(ar.shape[-1])  # fail fast on the 64-bit cap
    ar, ai = np.broadcast_arrays(ar, ai)
    shape = np.broadcast_shapes(ar.shape[:-2], br.shape[:-2]) + br.shape[-2:]
    br, bi = np.broadcast_to(br, shape), np.broadcast_to(bi, shape)
    hook = residual_hook(ar, ai, br, bi, f) if explicit else None
    state = cg_init(br, bi, f)
    for n in range(1, int(nit) + 1):
        sr, si = mm_step(ar, ai, state.pr, state.pi, f)
        state, scalars = vec_step(state, sr, si, f, residual=hook)
        if on_iteration is not None:
            on_iteration(n, state, scalars)
    return state
