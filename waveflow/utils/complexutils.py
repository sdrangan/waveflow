"""Vectorized complex value core over float / fixed / int inners.

numpy has **no integer-complex dtype**, so there are two representations:

- **float inner**  -> native numpy complex (``complex64`` / ``complex128``).
- **fixed / int**  -> a numpy **structured** dtype ``[('re', D), ('im', D)]`` of the
  **stored integers** (``D = int64`` signed / ``uint64`` unsigned), interleaved re/im in
  memory.  ``v['re']`` / ``v['im']`` are int-array views -> vectorized, loop-free, and the
  same I/Q layout ``std::complex<T>`` uses.

Complex arithmetic **composes** the fixed-point integer core
(:mod:`waveflow.utils.fixputils` ``add``/``mult``/``sub``) on the re/im stored-int
components -- an integer inner is just a :class:`Format` with ``frac_bits == 0`` -- and
numpy float ops for float (the **naive** multiply formula, not numpy's FMA complex `*`,
so it is bit-exact with ``std::complex<float>``; see :func:`cmult_float`).  **No
reimplemented fixed-point math.**  Result formats follow the
``FixedField`` rules and inherit the single-64-bit dtype + fail-fast >64 guard from
:class:`Format`:

==========  ==================================================  ======================
op          result inner format                                 note
==========  ==================================================  ======================
``cadd``    ``add_format(a, b)``                                int bits grow by 1
``csub``    ``sub_format(a, b)``                                always signed
``cmult``   ``sub_format(P, P)``, ``P = mult_format(a, b)``     ``(2W+1, 2I+1, signed)``
``conj``    ``sub_format(a, a)``                                ``(W+1, I+1, signed)``
==========  ==================================================  ======================

``cmult`` and ``conj`` produce signed results (a difference of products / negated imag),
so an **unsigned** inner raises (v1).  ``cadd`` keeps the inner signedness rule; ``csub``
is always signed.  Mixed signed/unsigned operands raise (inherited from ``*_format``).
"""
from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from waveflow.utils.fixputils import (
    Format, add, add_format, mult, mult_format, quantize, sub, sub_format,
)


# --- representations ----------------------------------------------------------
def int_format(W: int, signed: bool = True) -> Format:
    """The :class:`Format` of an integer inner (``frac_bits == 0`` -> ``I == W``)."""
    return Format(W, W, signed)


def complex_dtype(fmt: Format) -> np.dtype:
    """The structured stored-int dtype ``[('re', D), ('im', D)]`` for a fixed/int inner."""
    return np.dtype([("re", fmt.dtype), ("im", fmt.dtype)])


def make_complex(re: NDArray, im: NDArray, fmt: Format) -> NDArray:
    """Pack re/im stored-int arrays into a structured complex array of ``fmt``."""
    re = np.asarray(re, dtype=fmt.dtype)
    im = np.asarray(im, dtype=fmt.dtype)
    if re.shape != im.shape:
        raise ValueError(f"re/im shape mismatch: {re.shape} vs {im.shape}")
    out = np.empty(re.shape, dtype=complex_dtype(fmt))
    out["re"] = re
    out["im"] = im
    return out


def re_of(v: NDArray) -> NDArray:
    """The real stored-int component view of a structured complex array."""
    return np.asarray(v)["re"]


def im_of(v: NDArray) -> NDArray:
    """The imag stored-int component view of a structured complex array."""
    return np.asarray(v)["im"]


# --- result-format derivation (reuses the FixedField rules) -------------------
def cadd_format(a: Format, b: Format) -> Format:
    return add_format(a, b)


def csub_format(a: Format, b: Format) -> Format:
    return sub_format(a, b)


def cmult_format(a: Format, b: Format) -> Format:
    """``(ar*br - ai*bi)`` / ``(ar*bi + ai*br)`` -> ``sub_format(P, P)`` over ``P = a*b``.

    For same-sign signed inners this is ``(2W+1, 2I+1, signed)`` and the add half
    (``add_format(P, P)``) coincides with it, so re and im share one format."""
    p = mult_format(a, b)
    return sub_format(p, p)


def conj_format(a: Format) -> Format:
    """imag is negated -> grow by an int bit and force signed: ``(W+1, I+1, signed)``."""
    return sub_format(a, a)


# --- fixed/int complex arithmetic (composes fixputils on re/im) ---------------
def _require_signed(fmt: Format, op: str) -> None:
    if not fmt.signed:
        raise NotImplementedError(
            f"complex {op} produces signed results; use a signed inner (got unsigned).")


def cadd(va: NDArray, a: Format, vb: NDArray, b: Format) -> tuple[NDArray, Format]:
    """``(ar+br) + j(ai+bi)`` -- component ``add`` (full precision, int bits +1)."""
    re, r = add(re_of(va), a, re_of(vb), b)
    im, _ = add(im_of(va), a, im_of(vb), b)
    return make_complex(re, im, r), r


def csub(va: NDArray, a: Format, vb: NDArray, b: Format) -> tuple[NDArray, Format]:
    """``(ar-br) + j(ai-bi)`` -- component ``sub`` (full precision, always signed)."""
    re, r = sub(re_of(va), a, re_of(vb), b)
    im, _ = sub(im_of(va), a, im_of(vb), b)
    return make_complex(re, im, r), r


def cmult(va: NDArray, a: Format, vb: NDArray, b: Format) -> tuple[NDArray, Format]:
    """``(ar*br - ai*bi) + j(ar*bi + ai*br)`` -- four component ``mult`` + an ``add``/``sub``.

    Composes :mod:`fixputils` exactly, so the result is bit-identical to the explicit
    ``ap_fixed`` / ``ap_int`` component kernel (full-precision products, no rounding)."""
    _require_signed(a, "multiply")
    _require_signed(b, "multiply")
    ar, ai = re_of(va), im_of(va)
    br, bi = re_of(vb), im_of(vb)
    p_rr, p = mult(ar, a, br, b)        # ar*br
    p_ii, _ = mult(ai, a, bi, b)        # ai*bi
    p_ri, _ = mult(ar, a, bi, b)        # ar*bi
    p_ir, _ = mult(ai, a, br, b)        # ai*br
    re, r = sub(p_rr, p, p_ii, p)       # ar*br - ai*bi  -> sub_format(P, P)
    im, _ = add(p_ri, p, p_ir, p)       # ar*bi + ai*br  -> add_format(P, P) (== r, signed)
    return make_complex(re, im, r), r


def cmult3(va: NDArray, a: Format, vb: NDArray, b: Format) -> tuple[NDArray, Format]:
    """The same exact product as :func:`cmult`, in the three-multiply (Gauss) form.

    ``k1 = br(ar + ai)``, ``k2 = ar(bi - br)``, ``k3 = ai(br + bi)``; ``re = k1 - k3``,
    ``im = k1 + k2``.  Every term is exact, so the values equal :func:`cmult`'s, and they are
    returned in :func:`cmult_format`, which holds them exactly -- the twin of ``cmult3`` in
    ``complex_utils.hpp``."""
    _require_signed(a, "multiply")
    _require_signed(b, "multiply")
    ar, ai = re_of(va), im_of(va)
    br, bi = re_of(vb), im_of(vb)
    s1, f1 = add(ar, a, ai, a)          # ar + ai
    k1, g1 = mult(br, b, s1, f1)        # br (ar + ai)
    s2, f2 = sub(bi, b, br, b)          # bi - br
    k2, g2 = mult(ar, a, s2, f2)        # ar (bi - br)
    s3, f3 = add(br, b, bi, b)          # br + bi
    k3, g3 = mult(ai, a, s3, f3)        # ai (br + bi)
    re, fr = sub(k1, g1, k3, g3)
    im, fi = add(k1, g1, k2, g2)
    r = cmult_format(a, b)
    return make_complex(quantize(re, fr, r), quantize(im, fi, r), r), r


def conj(va: NDArray, a: Format) -> tuple[NDArray, Format]:
    """``ar - j(ai)`` -- imag negated; result ``(W+1, I+1, signed)`` (lossless re widen)."""
    _require_signed(a, "conjugate")
    re_in, im_in = re_of(va), im_of(va)
    zeros = np.zeros_like(np.asarray(re_in))
    re, r = add(re_in, a, zeros, a)     # widen re losslessly to (W+1, I+1, signed)
    im, _ = sub(zeros, a, im_in, a)     # -ai in the same format
    return make_complex(re, im, r), r


# --- float complex arithmetic (numpy float ops; no growth) --------------------
# add/sub/conj are componentwise (one IEEE-rounded op per component) and match
# std::complex<T> bit-for-bit.  multiply is the EDGE: numpy's complex-dtype `*` is
# FMA-based and diverges from std::complex<T>'s naive (ar*br - ai*bi) formula on ~25%
# of random operands -- so cmult uses the **naive float formula** (three IEEE-rounded
# float ops per component, same order as libstdc++ operator*), which is bit-exact with
# Vitis std::complex<float>/<double> (verified on random operands).  Raw numpy `*` (FMA)
# stays available via `.val`.
def cadd_float(a: NDArray, b: NDArray) -> NDArray:
    return np.asarray(a) + np.asarray(b)


def csub_float(a: NDArray, b: NDArray) -> NDArray:
    return np.asarray(a) - np.asarray(b)


def cmult_float(a: NDArray, b: NDArray) -> NDArray:
    a, b = np.asarray(a), np.asarray(b)
    ft = a.real.dtype                               # float32 / float64 (no FMA, no upcast)
    re = (a.real * b.real - a.imag * b.imag).astype(ft)
    im = (a.real * b.imag + a.imag * b.real).astype(ft)
    out = np.empty(np.broadcast(re, im).shape, dtype=np.result_type(a.dtype, b.dtype))
    out.real, out.imag = re, im
    return out


def conj_float(a: NDArray) -> NDArray:
    return np.conj(np.asarray(a))
