"""ltbf_fixed.py -- the bit-exact fixed-point LTBF CG solve, on waveflow.linalg's models.

``plans/mimo_cg/ltbf_cg_dse.md`` step 2.3.  The vector steps are :mod:`waveflow.linalg.cg` as they
stand (``cg_init``, ``vec_step``: the multi-RHS CG with its guarded divisions); only the matrix
product ``S`` differs, and it is built from :func:`waveflow.linalg.matmul.matmul`, exact before one
rounding:

* **explicit:** ``Q = q_Q(I + H_alpha H_alpha^H)`` once, then ``S = q_S(Q D)`` per iteration;
* **factored:** ``T = q_T(H_alpha^H D)``, then ``S = q_S(D + H_alpha T)``, ``Q`` never formed.

Every register is ``ap_fixed<W, I, AP_RND, AP_SAT>`` (the previous study's convention).  The
integer bits ``I`` of each register come from a range profile of the float CG over the drops plus
``guard`` bits (negative for registers below 1/4) (:func:`profile_ranges`, :func:`formats_for`); the vectors take the word width ``W``,
the column scalars (``ps``, ``rz``, ``alpha``, ``beta``) ``min(W + SCALAR_EXTRA, SCALAR_MAX)`` bits.  The engine's
output is ``X`` (``A_i`` in regime 1); ``M_i``, its eigendecomposition and ``G_i`` are the A53's,
in floating point.

**Provenance.** Copied from ``paper/cg_dse`` (``examples/ltbf_cg/``, ``ba757884``) by
``plans/ltbf_dse/ltbf_dse.md`` step 0.5; the ``plans/mimo_cg/ltbf_cg_dse.md`` it cites lives on that
branch (``git show paper/cg_dse:plans/mimo_cg/ltbf_cg_dse.md``).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from examples.ltbf_dse import ltbf_link as lk
from waveflow.linalg import cg as lcg
from waveflow.linalg.matmul import acc_format
from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format, OMode, QMode

#: The registers a format is chosen for.
VECTORS = ("H", "Q", "B", "P", "R", "S", "X", "T")
SCALARS = ("ps", "rz", "alpha", "beta")
#: Extra bits of the column scalars over the vector word width, and their cap: the exact quotient
#: of ``rz'/rz`` is ``1 + (Ws + G_DIV) + (Ws - I_rz)`` bits, which must stay within 64.
SCALAR_EXTRA = 8
SCALAR_MAX = 28
#: Fraction bits the dividend of both divisions is widened by (exact).
G_DIV = 6


def reg(W: int, I: int) -> Format:
    """A register format: signed, ``AP_RND``, ``AP_SAT``."""
    return Format(int(W), int(I), True, QMode.AP_RND, OMode.AP_SAT)


def profile_ranges(h_alpha: np.ndarray, b: np.ndarray, t_max: int) -> dict[str, float]:
    """The largest magnitude of each register's real or imaginary part over a float CG run
    (explicit and factored share every register but ``Q``/``T``; both are recorded)."""
    m = {k: 0.0 for k in VECTORS + SCALARS}

    def note(name, v):
        v = np.asarray(v)
        if v.size:
            m[name] = max(
                m[name], float(np.max(np.abs(v.real))), float(np.max(np.abs(v.imag)))
            )

    q = lk.q_hat(h_alpha)
    note("H", h_alpha)
    note("Q", q)
    note("B", b)
    x = np.zeros_like(b, dtype=complex)
    r = b.astype(complex)
    d = r.copy()
    rs = np.einsum("ij,ij->j", r.conj(), r).real
    note("R", r)
    note("P", d)
    note("rz", rs)
    for _ in range(int(t_max)):
        t = h_alpha.conj().T @ d
        s = d + h_alpha @ t
        note("T", t)
        note("S", s)
        ds = np.einsum("ij,ij->j", d.conj(), s).real
        note("ps", ds)
        om = np.divide(rs, ds, out=np.zeros_like(rs), where=ds != 0)
        note("alpha", om)
        x = x + d * om
        r = r - s * om
        rs_new = np.einsum("ij,ij->j", r.conj(), r).real
        nu = np.divide(rs_new, rs, out=np.zeros_like(rs), where=rs != 0)
        note("beta", nu)
        d = r + d * nu
        rs = rs_new
        note("X", x)
        note("R", r)
        note("P", d)
        note("rz", rs)
    return m


def profile_link(
    lt: lk.LongTerm, t_max: int, regimes: tuple[int, ...] = (1, 2)
) -> dict[str, float]:
    """:func:`profile_ranges` over every sector of one long-term update and the given regimes (a
    regime is its own hardware: the sweep profiles one at a time)."""
    m = {k: 0.0 for k in VECTORS + SCALARS}
    for s, ha in enumerate(lt.h_alpha):
        if lt.own[s].size == 0:
            continue
        for regime in regimes:
            b = lt.rhs[s] if regime == 1 else np.eye(ha.shape[0], dtype=complex)
            for k, v in profile_ranges(ha, b, t_max).items():
                m[k] = max(m[k], v)
    return m


def merge_ranges(*profiles: dict[str, float]) -> dict[str, float]:
    """The register-wise maximum of several range profiles."""
    return {k: max(p.get(k, 0.0) for p in profiles) for k in VECTORS + SCALARS}


def int_bits(maxabs: float, guard: int) -> int:
    """Signed integer bits that hold ``+-maxabs``, plus ``guard``.  Negative for a register whose
    values stay below 1/4 (``ap_fixed`` allows ``I < 0``): power control holds the total SNR, so
    channel entries shrink as ``1/sqrt(N_rx)`` and a floor at 1 would waste bits on large arrays.
    """
    if maxabs <= 0:
        return 1 + int(guard)
    return math.floor(math.log2(maxabs)) + 2 + int(guard)


@dataclass(frozen=True)
class LtbfFormats:
    """The fixed-point formats of one LTBF engine configuration."""

    W: int
    guard: int
    H: Format
    Q: Format
    T: Format
    cg: lcg.CgFormats

    @property
    def name(self) -> str:
        return f"W{self.W}g{self.guard}"


def formats_for(W: int, ranges: dict[str, float], guard: int = 1) -> LtbfFormats:
    """Formats at word width ``W`` from a range profile, each register with ``guard`` bits.

    A register whose integer bits would leave fewer than 2 fraction bits is widened to keep 2; the
    scalars take ``min(W + SCALAR_EXTRA, SCALAR_MAX)`` bits."""

    def vec(name: str) -> Format:
        i = int_bits(ranges[name], guard)
        return reg(max(W, i + 2), i)

    def sca(name: str) -> Format:
        i = int_bits(ranges[name], guard)
        return reg(max(min(W + SCALAR_EXTRA, SCALAR_MAX), i + 2), i)

    cg = lcg.CgFormats(
        A=vec("Q"),
        B=vec("B"),
        P=vec("P"),
        R=vec("R"),
        S=vec("S"),
        X=vec("X"),
        ps=sca("ps"),
        rz=sca("rz"),
        alpha=sca("alpha"),
        beta=sca("beta"),
        g_div=G_DIV,
    )
    return LtbfFormats(int(W), int(guard), vec("H"), vec("Q"), vec("T"), cg)


def to_stored(x: np.ndarray, fmt: Format) -> tuple[np.ndarray, np.ndarray]:
    return fx.quantize_real(np.real(x), fmt), fx.quantize_real(np.imag(x), fmt)


def to_complex(re: np.ndarray, im: np.ndarray, fmt: Format) -> np.ndarray:
    return fx.to_float(re, fmt) + 1j * fx.to_float(im, fmt)


#: Bits a float64 GEMM result may use and stay exact (every partial sum is an integer below 2^53).
_F64_BITS = 52


def _gemm_exact(a: np.ndarray, b: np.ndarray, wa: int, wb: int) -> np.ndarray:
    """``a @ b`` of int64 matrices whose entries fit ``wa`` and ``wb`` signed bits, exactly.

    float64 BLAS is exact while every partial sum stays below ``2^53``: ``K`` products of at most
    ``2^(wa-1) 2^(wb-1)`` sum to below ``2^(wa+wb-2+ceil(log2 K))``.  Otherwise ``b`` is split
    into a signed high part and an unsigned low part (``b = hi 2^s + lo``) and the halves are
    recombined in int64, which the caller's 64-bit format check bounds."""
    k = a.shape[-1]
    grow = (k - 1).bit_length()
    if wa + wb - 2 + grow <= _F64_BITS:
        return np.rint(a.astype(np.float64) @ b.astype(np.float64)).astype(np.int64)
    s = max(1, _F64_BITS - (wa - 1) - grow)  # lo < 2^s keeps a @ lo exact
    if wa - 1 + (wb - s) + grow > _F64_BITS:
        raise NotImplementedError(
            f"no exact float64 split for {wa}x{wb} bits at K = {k}"
        )
    hi, lo = b >> s, b & ((1 << s) - 1)
    af = a.astype(np.float64)
    out_hi = np.rint(af @ hi.astype(np.float64)).astype(np.int64)
    out_lo = np.rint(af @ lo.astype(np.float64)).astype(np.int64)
    return (out_hi << s) + out_lo


def mm_exact(ar, ai, a: Format, br, bi, b: Format, *, adjoint: bool = False):
    """:func:`waveflow.linalg.matmul.matmul_exact`, bit for bit, on float64 BLAS (form 4)."""
    if adjoint:
        ar, ai = np.swapaxes(ar, -1, -2), -np.swapaxes(ai, -1, -2)
    fmt = acc_format(a, b, ar.shape[-1])
    wa, wb = a.W + (1 if adjoint else 0), b.W  # -ai may need one more bit
    re = _gemm_exact(ar, br, wa, wb) - _gemm_exact(ai, bi, wa, wb)
    im = _gemm_exact(ar, bi, wa, wb) + _gemm_exact(ai, br, wa, wb)
    return re, im, fmt


def mm(ar, ai, a: Format, br, bi, b: Format, c: Format, *, adjoint: bool = False):
    """``q_c(A B)`` (or ``q_c(A^H B)``): :func:`waveflow.linalg.matmul.matmul`, bit for bit."""
    re, im, fmt = mm_exact(ar, ai, a, br, bi, b, adjoint=adjoint)
    return fx.quantize(re, fmt, c), fx.quantize(im, fmt, c)


def q_explicit(
    hr: np.ndarray, hi: np.ndarray, f: LtbfFormats
) -> tuple[np.ndarray, np.ndarray]:
    """``Q = q_Q(I + H_alpha H_alpha^H)``, exact before the one rounding.

    ``H H^H`` is the conjugate of ``(H^T)^H H^T``, so the stored ``H`` is never negated (the
    most negative stored value has no negation in its format)."""
    gr, gi, gfmt = mm_exact(hr.T, hi.T, f.H, hr.T, hi.T, f.H, adjoint=True)
    gr, gi = gr.copy(), -gi
    gr[np.diag_indices(gr.shape[0])] += np.int64(1) << gfmt.frac_bits
    return fx.quantize(gr, gfmt, f.Q), fx.quantize(gi, gfmt, f.Q)


def s_factored(hr, hi, pr, pi, f: LtbfFormats) -> tuple[np.ndarray, np.ndarray]:
    """``S = q_S(D + H_alpha q_T(H_alpha^H D))``."""
    c = f.cg
    tr, ti = mm(hr, hi, f.H, pr, pi, c.P, f.T, adjoint=True)
    ur, ui, ufmt = mm_exact(hr, hi, f.H, tr, ti, f.T)
    sr, sfmt = fx.add(pr, c.P, ur, ufmt)
    si, _ = fx.add(pi, c.P, ui, ufmt)
    return fx.quantize(sr, sfmt, c.S), fx.quantize(si, sfmt, c.S)


def solve_stored(
    hr: np.ndarray,
    hi: np.ndarray,
    br: np.ndarray,
    bi: np.ndarray,
    f: LtbfFormats,
    t_max: int,
    form: str = "factored",
    on_iteration: Callable[[int, lcg.CgState, dict], None] | None = None,
) -> lcg.CgState:
    """The fixed-point multi-RHS CG of ``Q X = B`` from stored ``H`` and ``B``: the state after
    ``t_max`` iterations (the engine's golden).  ``on_iteration(n, state, scalars)`` sees the
    stored state after iteration ``n``."""
    c = f.cg
    if form == "explicit":
        qr, qi = q_explicit(hr, hi, f)

        def product(pr, pi):
            return mm(qr, qi, f.Q, pr, pi, c.P, c.S)

    elif form == "factored":

        def product(pr, pi):
            return s_factored(hr, hi, pr, pi, f)

    else:
        raise ValueError(f"form must be 'explicit' or 'factored', got {form!r}")
    state = lcg.cg_init(br, bi, c)
    for n in range(1, int(t_max) + 1):
        sr, si = product(state.pr, state.pi)
        state, scalars = lcg.vec_step(state, sr, si, c)
        if on_iteration is not None:
            on_iteration(n, state, scalars)
    return state


def solve_fixed(
    h_alpha: np.ndarray,
    b: np.ndarray,
    f: LtbfFormats,
    t_max: int,
    form: str = "factored",
    on_iteration: Callable[[int, lcg.CgState, dict], None] | None = None,
) -> list[np.ndarray]:
    """The fixed-point multi-RHS CG of ``Q X = B``: ``X_1 .. X_t_max`` as floats.

    ``on_iteration(n, state, scalars)`` sees the stored state after iteration ``n`` (the
    conformance golden)."""
    c = f.cg
    hr, hi = to_stored(h_alpha, f.H)
    br, bi = to_stored(b, c.B)
    out: list[np.ndarray] = []

    def keep(n, state, scalars):
        if on_iteration is not None:
            on_iteration(n, state, scalars)
        out.append(to_complex(state.xr, state.xi, c.X))

    solve_stored(hr, hi, br, bi, f, t_max, form, keep)
    return out


def check_widths(f: LtbfFormats, n_rx: int, n_cols: int) -> None:
    """Fail fast if any exact intermediate of this configuration exceeds 64 bits."""
    f.cg.intermediates(n_rx)
    acc_format(f.H, f.cg.P, n_rx)  # T = H^H D
    acc_format(f.H, f.T, n_cols)  # H T
    acc_format(f.H, f.H, n_cols)  # H H^H


def sector_a_fixed(
    lt: lk.LongTerm,
    s: int,
    f: LtbfFormats,
    t_max: int,
    form: str = "factored",
    regime: int = 1,
) -> dict[int, list[np.ndarray]]:
    """Per own UE of sector ``s``: the fixed-point ``A_i`` for every ``t``, as floats."""
    ha, own = lt.h_alpha[s], lt.own[s]
    if own.size == 0:
        return {}
    b = lt.rhs[s] if regime == 1 else np.eye(ha.shape[0], dtype=complex)
    xs = solve_fixed(ha, b, f, t_max, form)
    out = {}
    for k, u in enumerate(own):
        per_t = []
        for x in xs:
            if regime == 1:
                per_t.append(x[:, k * lk.N_SRS : (k + 1) * lk.N_SRS])
            else:
                per_t.append(0.5 * (x + x.conj().T) @ lt.srs_own[int(u)])
        out[int(u)] = per_t
    return out
