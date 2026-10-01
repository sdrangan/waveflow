"""Integer-backed, Vitis-bit-exact fixed-point numeric core.

A value in ``ap_fixed<W, I, Q, O>`` is the stored W-bit integer ``s`` interpreted as
``s · 2**-(W-I)``.  This module is the pure-numeric engine: the ``QMode``/``OMode``
enums, the ``Format`` descriptor (with the fail-fast width guard), float<->stored
quantization, the integer requantizer, and the vectorized integer arithmetic
primitives (``mult``/``add``/``sub``/``shift``/``fixed_sum``) with their result-format
derivation.  No DataSchema dependency — unit-testable in isolation against an exact
``Fraction`` oracle.

dtype strategy (decision 5): a single 64-bit numpy int dtype — ``int64`` for signed
formats, ``uint64`` for unsigned (a W-bit value fits iff ``W <= 64``).  Any format
width, declared OR derived by an op, that exceeds 64 raises ``NotImplementedError``
at the *format-derivation* step — numpy int64/uint64 silently wrap on overflow, so
the guard is a compile-time width check, never a runtime hope.  Ops are implemented
wrap-free at ``W = 64`` (same-sign operands; ``AP_RND`` via ``floor + round-bit``,
not an overflowing add).  Wide (>64-bit) support is Future, via the ``(n, k)``
uint64-word convention.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

MAX_WIDTH = 64


class QMode(enum.Enum):
    """Quantization mode (value = the Vitis ``ap_fixed`` template token)."""
    AP_TRN = "AP_TRN"    # round toward minus infinity (floor) — Vitis default
    AP_RND = "AP_RND"    # round half up (ties toward plus infinity)


class OMode(enum.Enum):
    """Overflow mode (value = the Vitis ``ap_fixed`` template token)."""
    AP_WRAP = "AP_WRAP"  # two's-complement wrap (mask) — Vitis default
    AP_SAT = "AP_SAT"    # saturate (clip to [min, max])


@dataclass(frozen=True)
class Format:
    """A fixed-point format ``ap_fixed<W, I, q, o>`` (or ``ap_ufixed`` if unsigned).

    ``W`` total bits, ``int_bits`` = I, ``frac_bits`` = F = W - I.  Construction is
    where the width guard fires (decision 5): any ``W > MAX_WIDTH`` — declared or
    derived by an op — raises ``NotImplementedError`` before any numpy op can wrap.
    """
    W: int
    int_bits: int                       # I (integer bits, sign-inclusive when signed)
    signed: bool = True
    q_mode: QMode = QMode.AP_TRN
    o_mode: OMode = OMode.AP_WRAP

    def __post_init__(self) -> None:
        if self.W <= 0:
            raise ValueError(f"W (total bits) must be positive, got {self.W}.")
        if self.W > MAX_WIDTH:
            raise NotImplementedError(
                f"fixed-point format width W={self.W} exceeds the {MAX_WIDTH}-bit v1 "
                "limit (numpy int64/uint64 would silently wrap). Wide (>64-bit) support "
                "via the (n, k) uint64-word convention is Future.")

    @property
    def frac_bits(self) -> int:
        return self.W - self.int_bits

    @property
    def dtype(self) -> type:
        return np.int64 if self.signed else np.uint64


def _require_same_sign(a: Format, b: Format) -> None:
    if a.signed != b.signed:
        raise NotImplementedError(
            "mixed signed/unsigned fixed-point arithmetic is not supported in v1 "
            "(numpy would coerce int64/uint64 to float64). Promote both operands to a "
            "common signed format first.")


# --- overflow primitives (W <= 64 safe; >= 64 wrap/sat is identity in-dtype) --
def truncate(
    x: NDArray[np.int64] | int,
    wid: int = 32,
    signed: bool = True,
) -> NDArray[np.int64] | int:
    """Wrap integers to a ``wid``-bit two's-complement (``AP_WRAP``)."""
    arr = np.asarray(x)
    if wid >= MAX_WIDTH:
        return arr                              # already within the 64-bit dtype
    mask = (1 << wid) - 1
    y = (arr.astype(np.int64) & mask)
    if signed:
        # Sign-extend as (y ^ s) - s: every intermediate stays inside int64, even at
        # wid == 63, where the textbook ``y - (1 << wid)`` overflows.
        signbit = 1 << (wid - 1)
        y = (y ^ signbit) - signbit
    return y


def saturate(
    x: NDArray[np.int64] | int,
    wid: int = 32,
    signed: bool = True,
) -> NDArray[np.int64] | int:
    """Saturate integers to the ``wid``-bit range (``AP_SAT``)."""
    arr = np.asarray(x)
    if wid >= MAX_WIDTH:
        return arr                              # range == dtype range -> identity
    if signed:
        lo, hi = -(1 << (wid - 1)), (1 << (wid - 1)) - 1
    else:
        lo, hi = 0, (1 << wid) - 1
    return np.clip(arr, lo, hi)


def _apply_overflow(q: NDArray, fmt: Format) -> NDArray:
    y = truncate(q, fmt.W, fmt.signed) if fmt.o_mode == OMode.AP_WRAP \
        else saturate(q, fmt.W, fmt.signed)
    return np.asarray(y).astype(fmt.dtype)


# --- quantization -------------------------------------------------------------
def quantize_real(values: NDArray[np.float64] | float, fmt: Format) -> NDArray:
    """Quantize real value(s) into ``fmt``'s stored integer (float -> fixed).

    For loading real data into a format. ``scaled = value * 2**F`` is an exact
    float64 exponent shift for the v1 input range (``F`` modest); the only lossy
    step is the rounding the mode selects. Returns an array in ``fmt.dtype``."""
    scaled = np.asarray(values, dtype=np.float64) * (2.0 ** fmt.frac_bits)
    q = np.floor(scaled) if fmt.q_mode == QMode.AP_TRN else np.floor(scaled + 0.5)
    return _apply_overflow(q.astype(np.int64), fmt)


def quantize(stored: NDArray, src: Format, target: Format) -> NDArray:
    """Requantize stored integers from ``src`` to ``target`` format (fixed -> fixed).

    The lossy arithmetic step. Integer-exact (no float). With ``dF = Fsrc - Ftarget``:
    ``dF <= 0`` shifts left (target keeps more fraction bits, exact); ``dF > 0``
    rounds — ``AP_TRN`` = arithmetic ``>>`` (floor toward -inf), ``AP_RND`` =
    ``floor + round-bit`` (the wrap-free round-half-up). Then overflow to W."""
    s = np.asarray(stored)
    dF = src.frac_bits - target.frac_bits
    if dF <= 0:
        intermediate_width = src.int_bits + target.frac_bits
        if intermediate_width > MAX_WIDTH:
            raise NotImplementedError(
                f"requantize up-scale intermediate width {intermediate_width} exceeds "
                f"{MAX_WIDTH} bits; wide support is Future.")
        q = s.astype(target.dtype) << (-dF)
    else:
        floor = s >> dF                                   # arithmetic (signed) -> floor
        if target.q_mode == QMode.AP_RND:
            q = floor + ((s >> (dF - 1)) & 1)             # wrap-free round half up
        else:
            q = floor
    return _apply_overflow(q, target)


# --- views --------------------------------------------------------------------
def to_float(stored: NDArray | int, fmt: Format) -> NDArray[np.float64] | float:
    """Real-valued view ``stored * 2**-F`` (exact for the v1 range)."""
    arr = np.asarray(stored, dtype=np.float64)
    out = arr * (2.0 ** (-fmt.frac_bits))
    return float(out) if arr.ndim == 0 else out


def to_bits(stored: NDArray | int, W: int) -> NDArray[np.uint64] | int:
    """The raw ``W``-bit unsigned pattern (Vitis ``.range()``); two's-complement."""
    if W <= 0:
        raise ValueError(f"W must be positive, got {W}.")
    arr = np.asarray(stored)
    bits64 = arr.astype(np.int64).view(np.uint64) if np.issubdtype(arr.dtype, np.signedinteger) \
        else arr.astype(np.uint64)
    if W >= 64:
        out = bits64
    else:
        out = bits64 & np.uint64((1 << W) - 1)
    return int(out) if arr.ndim == 0 else out


# --- format derivation (the width guard fires inside Format(...)) -------------
def mult_format(a: Format, b: Format) -> Format:
    """``ap_fixed`` mult: ``<Wa+Wb, Ia+Ib>`` (fraction bits add); signed if either."""
    _require_same_sign(a, b)
    return Format(a.W + b.W, a.int_bits + b.int_bits, a.signed or b.signed)


def add_format(a: Format, b: Format) -> Format:
    """``ap_fixed`` add: align fractions (F = max), grow integer bits by 1 (carry)."""
    _require_same_sign(a, b)
    frac = max(a.frac_bits, b.frac_bits)
    ints = max(a.int_bits, b.int_bits) + 1
    return Format(ints + frac, ints, a.signed or b.signed)


def sub_format(a: Format, b: Format) -> Format:
    """``ap_fixed`` sub: like add, but the result is always signed."""
    _require_same_sign(a, b)
    frac = max(a.frac_bits, b.frac_bits)
    ints = max(a.int_bits, b.int_bits) + 1
    return Format(ints + frac, ints, True)


def shift_format(a: Format, n: int) -> Format:
    """Rescale by ``2**n`` (point-move): same W/bits, I grows by n, F shrinks by n."""
    return Format(a.W, a.int_bits + n, a.signed, a.q_mode, a.o_mode)


def div_format(a: Format, b: Format) -> Format:
    """``ap_fixed`` division: ``<Sb + Wa + max(Fb, 0), Sb + Ia + Fb>``, signed if either.

    The rule of Vitis 2024.1 ``ap_fixed_base.h`` (``RType::div``, non-``__SC_COMPATIBLE__``;
    ``Sb`` = 1 for a signed divisor).  The quotient keeps the **dividend's** fraction bits
    (``F = Fa`` for ``Fb >= 0``).  For a more precise quotient, widen the dividend first:
    quantize it to more fraction bits, which is exact.
    """
    _require_same_sign(a, b)
    sb = 1 if b.signed else 0
    fb = b.frac_bits
    return Format(sb + a.W + max(fb, 0), sb + a.int_bits + fb, a.signed or b.signed)


def sum_format(a: Format, n_terms: int) -> Format:
    """Reduction of ``n_terms`` values: grow integer bits by ``ceil(log2(n_terms))``."""
    if n_terms < 1:
        raise ValueError(f"n_terms must be >= 1, got {n_terms}.")
    growth = (n_terms - 1).bit_length()         # ceil(log2(n_terms))
    ints = a.int_bits + growth
    return Format(ints + a.frac_bits, ints, a.signed, a.q_mode, a.o_mode)


# --- vectorized integer arithmetic primitives (stored-int arrays) -------------
def mult(sa: NDArray, a: Format, sb: NDArray, b: Format) -> tuple[NDArray, Format]:
    r = mult_format(a, b)
    out = np.asarray(sa).astype(r.dtype) * np.asarray(sb).astype(r.dtype)
    return out.astype(r.dtype), r


def add(sa: NDArray, a: Format, sb: NDArray, b: Format) -> tuple[NDArray, Format]:
    r = add_format(a, b)
    aa = np.asarray(sa).astype(r.dtype) << (r.frac_bits - a.frac_bits)
    bb = np.asarray(sb).astype(r.dtype) << (r.frac_bits - b.frac_bits)
    return (aa + bb).astype(r.dtype), r


def sub(sa: NDArray, a: Format, sb: NDArray, b: Format) -> tuple[NDArray, Format]:
    r = sub_format(a, b)
    aa = np.asarray(sa).astype(r.dtype) << (r.frac_bits - a.frac_bits)
    bb = np.asarray(sb).astype(r.dtype) << (r.frac_bits - b.frac_bits)
    return (aa - bb).astype(r.dtype), r


def div(sa: NDArray, a: Format, sb: NDArray, b: Format) -> tuple[NDArray, Format]:
    """``ap_fixed`` division with a zero guard.

    Vitis computes ``(a << max(Fb, 0)) / b`` as an integer division that truncates toward
    zero (``sdiv``/``udiv`` in ``ap_fixed_base.h``); the result has :func:`div_format`.
    ``ap_fixed`` leaves ``x / 0`` undefined, so Waveflow defines it as **0**: generated or
    hand-written C++ must guard the same way, ``(b == 0) ? 0 : a / b``.

    One Vitis C-simulation detail is reproduced exactly: the integer division runs at the
    shifted dividend's width ``max(Wa + max(Fb, 0), Wb)``, not at the wider result type, so
    the single overflowing case, the most negative dividend over a divisor of −1 LSB, wraps
    back to the most negative value.  This is C-simulation behaviour: the synthesis branch of
    ``ap_fixed_base.h`` leaves the case undefined, so RTL may differ.  (It cannot arise in the
    CG solve, whose dividends ``rᴴr`` are never negative.)
    """
    r = div_format(a, b)
    num = np.asarray(sa).astype(r.dtype) << max(b.frac_bits, 0)
    den = np.asarray(sb).astype(r.dtype)
    num, den = np.broadcast_arrays(num, den)
    zero = den == 0
    safe = np.where(zero, 1, den)
    if r.signed:
        # Truncation toward zero from floor division plus a remainder test: no abs(), which
        # overflows at -2**63 (a 64-bit signed divisor).  |num| <= 2**62, so neither
        # floor_divide nor remainder can overflow.
        floor = np.floor_divide(num, safe)
        inexact = np.remainder(num, safe) != 0
        quot = floor + (inexact & ((num < 0) != (safe < 0)))
        quot = truncate(quot, max(a.W + max(b.frac_bits, 0), b.W), signed=True)
    else:
        quot = num // safe
    return np.where(zero, 0, quot).astype(r.dtype), r


def shift(sa: NDArray, a: Format, n: int) -> tuple[NDArray, Format]:
    """Lossless point-move rescale: stored bits unchanged, value scaled by 2**n."""
    r = shift_format(a, n)
    return np.asarray(sa).astype(r.dtype).copy(), r


def fixed_sum(sa: NDArray, a: Format, axis: int | None = None) -> tuple[NDArray, Format]:
    """Full-precision reduction: sum stored ints, format grown by ceil(log2(N))."""
    arr = np.asarray(sa)
    n_terms = arr.size if axis is None else arr.shape[axis]
    r = sum_format(a, n_terms)
    out = arr.astype(r.dtype).sum(axis=axis)
    return np.asarray(out).astype(r.dtype), r
