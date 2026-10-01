"""mimo_cg_fixed.py — the bit-exact fixed-point CG detector.

Step 2.3 of ``plans/mimo_cg/mimo_cg_paper_sims.md``, implementing the gate 2.1 decisions
recorded in the plan's §14.  It is the golden model: every register value is an ``ap_fixed``
stored integer computed with :mod:`waveflow.utils.fixputils`, the core that is proven
bit-exact against Vitis.  The C++ reference of step 2.4 must reproduce it bit for bit.

The algorithm
-------------
Multi-RHS CG on ``(A/M) X = B/M``.  ``A = HᴴH + σ²I`` and ``B = HᴴY`` are formed in floating
point and quantized at the input (decision 6); M is a power of two, so the scaling is a
shift.  Every column of ``B`` has its own α and β.  Starting from ``X = 0``, ``R = B``,
``P = R``, ``rz = Σ|R|²``, each iteration performs, in this order (``q_V`` = quantize to the
register format of ``V``, AP_RND and AP_SAT; ``w`` = widen the dividend by ``g_div``
fraction bits, which is exact; ``/`` = :func:`~waveflow.utils.fixputils.div` with its zero
guard)::

    1. S     = q_S(A @ P)                     full-precision complex products, exact sum over K
    2. ps    = q_ps(Σ_k Re(conj(P) * S))      per column
    3. alpha = q_alpha(w(rz) / ps)            ps == 0 -> alpha = 0 (X unchanged this iteration)
    4. X     = q_X(X + P * alpha)
    5. R     = q_R(R - S * alpha)             recurrence, or q_R(B - A @ X) explicit
    6. rz'   = q_rz(Σ_k |R|²)
    7. beta  = q_beta(w(rz') / rz)            rz == 0 -> beta = 0
    8. rz    = rz'
    9. P     = q_P(R + P * beta)

Every product and sum before a ``q_`` is exact.  Integer arithmetic is associative, so any
summation order gives the same register values.  A column freezes only once ``rz == 0``:
then α = 0 (zero dividend), β = 0 (guard), and R, P stay zero.  A zero ``ps`` with a nonzero
``rz`` only skips that iteration's X update; β becomes 1 and CG continues.

"Bit-exact" covers the CG from the quantized A/M and B/M onward; the Gram matrix and the
matched filter are floating point (gate 2.1 decision 6).

Representation
--------------
Internally a complex register is a pair of int64 stored arrays ``(re, im)`` with one
:class:`~waveflow.utils.fixputils.Format`; this keeps the batched sweeps of Phase 3 fast.
:class:`CgFixedResult` exposes the stored values, the real view, and a
``DataArray[ComplexField]`` (:meth:`CgFixedResult.as_data_array`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields

import numpy as np

from waveflow.hw.complexfield import ComplexField
from waveflow.hw.dataschema import DataArray
from waveflow.hw.fixpoint import FixedField
from waveflow.utils import complexutils as cx
from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format, OMode, QMode

#: Integer bits per register, sign included (gate 2.1 decision 5, from the range re-profile).
INT_BITS = {
    "A": 3,
    "B": 4,
    "P": 4,
    "R": 4,
    "S": 5,
    "X": 3,
    "ps": 10,
    "rz": 9,
    "alpha": 5,
    "beta": 3,
}
VECTORS = ("A", "B", "P", "R", "S", "X")
SCALARS = ("ps", "rz", "alpha", "beta")


def register(W: int, I: int) -> Format:
    """A register format: signed, AP_RND, AP_SAT (gate 2.1 decision 5)."""
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


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

    @classmethod
    def from_width(cls, W: int, g_s: int = 0, g_div: int = 0) -> CgFormats:
        """Phase 3's parameterization: vectors are ``W`` wide, scalars ``W + g_s``."""
        regs = {v: register(W, INT_BITS[v]) for v in VECTORS}
        regs |= {s: register(W + g_s, INT_BITS[s]) for s in SCALARS}
        return cls(**regs, g_div=g_div)

    @classmethod
    def wide(cls) -> CgFormats:
        """AC2.4's wide reference (gate 2.1 decision 7): every register has 24 fraction bits.

        Validated against float CG at 64×8 (≤ 6e-5 from −10 to 20 dB).  At M/K = 2 it is
        looser: the M2 review measured up to 7.4e-4 at 32×16, 10 dB, mid-iteration.  So the
        no-quantization baseline for Phase 3 and the paper is float CG, not these formats.
        """
        return cls(
            A=register(28, 3),
            B=register(28, 4),
            P=register(28, 4),
            R=register(28, 4),
            S=register(29, 5),
            X=register(28, 3),
            ps=register(34, 10),
            rz=register(33, 9),
            alpha=register(29, 5),
            beta=register(27, 3),
            g_div=0,
        )

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
            "A@P": _matmul_format(f.A, f.P, K),
            "Re(P^H S)": _dot_format(f.P, f.S, K),
            "|R|^2": _dot_format(f.R, f.R, K),
            "rz/ps": fx.div_format(rz_w, f.ps),
            "rz'/rz": fx.div_format(rz_w, f.rz),
            "X+P*alpha": fx.add_format(f.X, fx.mult_format(f.P, f.alpha)),
            "R-S*alpha": fx.sub_format(f.R, fx.mult_format(f.S, f.alpha)),
            "B-A@X": fx.sub_format(f.B, _matmul_format(f.A, f.X, K)),
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
        "mm_ap": _matmul_format(f.A, f.P, K),
        "mm_ax": _matmul_format(f.A, f.X, K),
        "dot_ps": _dot_format(f.P, f.S, K),
        "dot_rz": _dot_format(f.R, f.R, K),
        "rzw": _widened(f.rz, f.g_div),
    }


def quantize_inputs(
    A: np.ndarray, B: np.ndarray, formats: CgFormats, scale: float = 1.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Stored integers ``(A_re, A_im, B_re, B_im)`` of ``A/scale`` and ``B/scale``.

    This is the only floating-point to fixed-point step; :func:`cg_fixed` and the C++
    conformance harness both use it, so they start from identical integers.
    """
    ar, ai = _from_real(np.asarray(A) / scale, formats.A)
    br, bi = _from_real(np.asarray(B) / scale, formats.B)
    return ar, ai, br, bi


def _widened(fmt: Format, g: int) -> Format:
    return Format(fmt.W + g, fmt.int_bits, fmt.signed, fmt.q_mode, fmt.o_mode)


def _matmul_format(a: Format, b: Format, K: int) -> Format:
    return fx.sum_format(cx.cmult_format(a, b), K)


def _dot_format(a: Format, b: Format, K: int) -> Format:
    p = fx.mult_format(a, b)
    return fx.sum_format(fx.add_format(p, p), K)


# --- register-level operations on (re, im) stored-int pairs --------------------------------


def _q(x: np.ndarray, src: Format, dst: Format) -> np.ndarray:
    return fx.quantize(x, src, dst)


def _from_real(z: np.ndarray, fmt: Format) -> tuple[np.ndarray, np.ndarray]:
    z = np.asarray(z)
    return fx.quantize_real(z.real, fmt), fx.quantize_real(z.imag, fmt)


def _matmul(
    ar, ai, fa: Format, br, bi, fb: Format
) -> tuple[np.ndarray, np.ndarray, Format]:
    """Exact complex matrix product (int64 matmul: integer sums are exact and order-free)."""
    K = ar.shape[-1]
    fmt = _matmul_format(fa, fb, K)
    re = ar @ br - ai @ bi
    im = ar @ bi + ai @ br
    return re, im, fmt


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


@dataclass
class CgFixedResult:
    """One fixed-point CG iterate ``X``: stored integers, format, and views."""

    re: np.ndarray
    im: np.ndarray
    fmt: Format

    @property
    def real(self) -> np.ndarray:
        """The complex floating-point value of every entry."""
        return fx.to_float(self.re, self.fmt) + 1j * fx.to_float(self.im, self.fmt)

    @property
    def stored(self) -> np.ndarray:
        """Structured ``[('re', int64), ('im', int64)]`` stored integers, shape preserved."""
        return cx.make_complex(self.re, self.im, self.fmt)

    def as_data_array(self) -> DataArray:
        """The values as a flat ``DataArray[ComplexField[FixedField<fmt>]]``."""
        f = self.fmt
        inner = FixedField.specialize(f.W, f.int_bits, f.signed, f.q_mode, f.o_mode)
        flat = self.stored.reshape(-1)
        return DataArray.specialize(
            ComplexField.specialize(inner), max_shape=flat.shape
        )(flat)


def cg_fixed(
    A: np.ndarray,
    B: np.ndarray,
    nit: int,
    formats: CgFormats,
    *,
    scale: float = 1.0,
    explicit_residual: bool = False,
    iterates: Iterable[int] | None = None,
    on_iteration: Callable[[int, dict[str, tuple]], None] | None = None,
) -> CgFixedResult | dict[int, CgFixedResult]:
    """Bit-exact fixed-point multi-RHS CG on ``(A/scale) X = B/scale``, from ``X = 0``.

    Parameters
    ----------
    A, B : complex ndarray, shapes ``(..., K, K)`` and ``(..., K, N)``
        Floating-point system; quantized to ``formats.A``/``formats.B`` after dividing by
        ``scale`` (pass ``scale = M``).
    nit : int
        Iteration count.
    formats : CgFormats
        Register formats; checked against the 64-bit cap before any arithmetic.
    explicit_residual : bool, optional
        Use ``R = B − A X`` instead of the recurrence ``R ← R − α S``.
    iterates : iterable of int, optional
        Return ``{n: X_n}`` for these ``n`` (``0 <= n <= nit``) instead of ``X_nit``.
    on_iteration : callable, optional
        ``on_iteration(n, regs)`` after each iteration, with ``regs[name] = (stored, Format)``
        for ``alpha``, ``beta``, ``ps``, ``rz`` and ``(re, im, Format)`` for ``X``, ``R``, ``P``.
        Used by the conformance harness to compare every register.

    Returns
    -------
    CgFixedResult or dict
    """
    f = formats
    K = A.shape[-1]
    f.intermediates(K)  # fail fast on the 64-bit cap
    wanted = None if iterates is None else set(iterates)
    if wanted is not None and any(n < 0 or n > nit for n in wanted):
        raise ValueError(f"iterates must lie in [0, {nit}], got {sorted(wanted)}")

    ar, ai, br, bi = quantize_inputs(A, B, f, scale)
    ar, ai = np.broadcast_arrays(ar, ai)
    shape = np.broadcast_shapes(ar.shape[:-2], br.shape[:-2]) + br.shape[-2:]
    br, bi = np.broadcast_to(br, shape), np.broadcast_to(bi, shape)

    xr = np.zeros(shape, np.int64)
    xi = np.zeros(shape, np.int64)
    rr, ri = _q(br, f.B, f.R), _q(bi, f.B, f.R)
    pr, pi = _q(rr, f.R, f.P), _q(ri, f.R, f.P)
    rz_full, rz_fmt = _column_dot(rr, ri, f.R, rr, ri, f.R)
    rz = _q(rz_full, rz_fmt, f.rz)
    rz_w_fmt = _widened(f.rz, f.g_div)

    out: dict[int, CgFixedResult] = {}
    if wanted is not None and 0 in wanted:
        out[0] = CgFixedResult(xr.copy(), xi.copy(), f.X)
    for n in range(1, nit + 1):
        sr, si, s_fmt = _matmul(ar, ai, f.A, pr, pi, f.P)  # 1
        sr, si = _q(sr, s_fmt, f.S), _q(si, s_fmt, f.S)
        ps_full, ps_fmt = _column_dot(pr, pi, f.P, sr, si, f.S)  # 2
        ps = _q(ps_full, ps_fmt, f.ps)
        q, q_fmt = fx.div(_q(rz, f.rz, rz_w_fmt), rz_w_fmt, ps, f.ps)  # 3
        alpha = _q(q, q_fmt, f.alpha)
        tr, ti, t_fmt = _scale(pr, pi, f.P, alpha, f.alpha)  # 4
        xr, xi, x_fmt = _addsub(fx.add, xr, xi, f.X, tr, ti, t_fmt)
        xr, xi = _q(xr, x_fmt, f.X), _q(xi, x_fmt, f.X)
        if explicit_residual:  # 5
            axr, axi, ax_fmt = _matmul(ar, ai, f.A, xr, xi, f.X)
            rr, ri, r_fmt = _addsub(fx.sub, br, bi, f.B, axr, axi, ax_fmt)
        else:
            tr, ti, t_fmt = _scale(sr, si, f.S, alpha, f.alpha)
            rr, ri, r_fmt = _addsub(fx.sub, rr, ri, f.R, tr, ti, t_fmt)
        rr, ri = _q(rr, r_fmt, f.R), _q(ri, r_fmt, f.R)
        rz_full, rz_fmt = _column_dot(rr, ri, f.R, rr, ri, f.R)  # 6
        rz_new = _q(rz_full, rz_fmt, f.rz)
        q, q_fmt = fx.div(_q(rz_new, f.rz, rz_w_fmt), rz_w_fmt, rz, f.rz)  # 7
        beta = _q(q, q_fmt, f.beta)
        rz = rz_new  # 8
        tr, ti, t_fmt = _scale(pr, pi, f.P, beta, f.beta)  # 9
        pr, pi, p_fmt = _addsub(fx.add, rr, ri, f.R, tr, ti, t_fmt)
        pr, pi = _q(pr, p_fmt, f.P), _q(pi, p_fmt, f.P)
        if on_iteration is not None:
            on_iteration(
                n,
                {
                    "ps": (ps, f.ps),
                    "alpha": (alpha, f.alpha),
                    "rz": (rz, f.rz),
                    "beta": (beta, f.beta),
                    "X": (xr, xi, f.X),
                    "R": (rr, ri, f.R),
                    "P": (pr, pi, f.P),
                },
            )
        if wanted is not None and n in wanted:
            out[n] = CgFixedResult(xr.copy(), xi.copy(), f.X)
    return out if wanted is not None else CgFixedResult(xr, xi, f.X)


# --- the link simulator with the bit-exact detector (step 2.5) ----------------------------


def simulate_point_fixed(
    cfg,
    rho_db: float,
    formats: dict[str, CgFormats],
    *,
    explicit_residual: bool = False,
    max_bits: int | None = None,
    min_errors: int | None = None,
) -> list[dict]:
    """BER of float CG and of the bit-exact CG in each format, on identical samples.

    Draws exactly the samples of :func:`examples.mimo_cg.mimo_cg.simulate_point` (same
    generator, same call order, same chunks); a test proves its float-CG error counts equal
    that function's.  For each CG iteration count ``n`` in ``cfg.nits`` it reports the float
    detector ``cg{n}`` and, per format, ``fx:{name}:cg{n}``, with ``mismatch``: the bits whose
    hard decision differs from float ``cg{n}``.  Estimates are unbiased by the same exact μ
    as the float runs (a simulation-side genie, see :mod:`examples.mimo_cg.detectors`).

    The stop rule is the float runs': every detector has ``min_errors`` bit errors, or
    ``max_bits`` bits have been simulated, checked at chunk boundaries.
    """
    import math

    from examples.mimo_cg import mimo_cg as base
    from examples.mimo_cg.detectors import bias_from_system, cg_multi_rhs
    from examples.mimo_cg.mimo_link import (
        Qam,
        noise_variance,
        point_rng,
        rayleigh,
        snr_key,
    )

    max_bits = base.MAX_BITS if max_bits is None else max_bits
    min_errors = base.MIN_ERRORS if min_errors is None else min_errors
    qam = Qam(cfg.order)
    b = qam.bits_per_symbol
    M, K = cfg.M, cfg.K
    sigma2 = noise_variance(rho_db)
    sigma = math.sqrt(sigma2)
    bits_per_block = base.NS * K * b
    chunk = max(1, base.CHUNK_BITS // bits_per_block)
    rng = point_rng(base._BER_STREAM, M, K, cfg.order, snr_key(rho_db))
    nits = cfg.nits
    names = [f"cg{n}" for n in nits] + [f"fx:{f}:cg{n}" for f in formats for n in nits]
    errors = dict.fromkeys(names, 0)
    mismatch = dict.fromkeys(names, 0)
    bits = blocks = 0
    eye = np.eye(K)
    while True:
        # The draw sequence of base.simulate_point, call for call.
        H = rayleigh(rng, (chunk, M, K))
        tx = rng.integers(0, 2, size=(chunk, base.NS, K * b), dtype=np.int8)
        X = np.swapaxes(qam.modulate(tx), -1, -2)
        Y = H @ X + sigma * rayleigh(rng, (chunk, M, base.NS))
        Hh = np.conj(np.swapaxes(H, -1, -2))
        A = Hh @ H + sigma2 * eye
        B = Hh @ Y
        mu = bias_from_system(A, sigma2)
        decisions = {}
        for n, Xn in cg_multi_rhs(A, B, max(nits), iterates=nits).items():
            decisions[f"cg{n}"] = qam.demodulate(np.swapaxes(Xn / mu, -1, -2))
        for fname, fmt in formats.items():
            fixed = cg_fixed(
                A,
                B,
                max(nits),
                fmt,
                scale=M,
                explicit_residual=explicit_residual,
                iterates=nits,
            )
            for n in nits:
                decisions[f"fx:{fname}:cg{n}"] = qam.demodulate(
                    np.swapaxes(fixed[n].real / mu, -1, -2)
                )
        for name, rx in decisions.items():
            errors[name] += int(np.count_nonzero(rx != tx))
            ref = decisions[name.rsplit(":", 1)[-1]] if name.startswith("fx:") else rx
            mismatch[name] += int(np.count_nonzero(rx != ref))
        bits += chunk * bits_per_block
        blocks += chunk
        if bits >= max_bits or min(errors.values()) >= min_errors:
            break
    return [
        {
            "M": M,
            "K": K,
            "modulation": cfg.modulation,
            "rho_db": rho_db,
            "detector": name,
            "bit_errors": errors[name],
            "mismatch": mismatch[name],
            "bits": bits,
            "blocks": blocks,
            "ber": errors[name] / bits,
        }
        for name in names
    ]
