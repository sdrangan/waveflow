"""Bit-exact model of ``xf::blas::gemv`` -- the Vitis BLAS L1 matrix-vector multiply.

The arithmetic here is ordinary float32 multiply and add.  What makes a naive model wrong is not
the operations but **the order they are applied in**: floating-point addition is not associative,
so a dot product's bits are decided by the reduction shape.  ``numpy.dot`` is wrong on half the
test cases and a plain binary tree on a fifth of them, while being defensible code in both cases.

## The structure, read from the library and confirmed by measurement

``gemv`` (``blas/L1/include/hw/xf_blas/gemv.hpp:47``) forwards to ``DotHelper::dot``, which
dispatches on the element type (``helpers/funcs/dotHelper.hpp:108-152``)::

    float, double     ->  dot_tree
    everything else   ->  dot_dsp        (not modelled yet -- S3)

``dot_tree`` is ``mul`` then ``sum``, and ``sum`` (``helpers/funcs/sum.hpp:104-118``) is three
stages, which together are *not* a plain tree::

    preProcess   BinarySum over each beat of ParEntries      -> one value per beat
    padding      pad the beat count up to a multiple of Delays
    postProcess  for each chunk of Delays beat-values:
                     BinarySum over the chunk                 (a tree)
                     finalSum += chunkResult                  (SEQUENTIAL)

So it is a tree *within* a beat, a tree *within* a chunk of beats, and a **sequential**
accumulation *across* chunks.  Three different orders in one reduction.

``Delays`` is the floating-point adder latency the design pipelines around
(``helpers/utils/utils.hpp:91-109``) -- **4 for float, 8 for double, 1 otherwise**.  It is not a
tuning knob a user sets; it is a property of the element type, and it changes the answer.

## Why the naive models pass small tests

A dot product only exposes its summation order when the partial sums differ in magnitude.  At
``M=4, N=16`` an early experiment matched a full tree, a per-beat tree, and the hardware all at
once -- and ``numpy.dot`` matched at ``N=64`` while differing at ``N=16``.  ``numpy.dot`` is
*unreliably* right, which is worse than reliably wrong: it will pass a small test suite and then
disagree in production.  The goldens here deliberately include wide dynamic range and cases where
the candidate models are known to differ.
"""
from __future__ import annotations

import struct

import numpy as np

#: ``AdderDelay<T>::m_Delays`` (``helpers/utils/utils.hpp:91-109``) -- the FP adder latency the
#: reduction pipelines around.  A property of the type, not a user parameter.
ADDER_DELAYS = {"float32": 4, "float64": 8}


def adder_delays(dtype: np.dtype | str = "float32") -> int:
    return ADDER_DELAYS.get(np.dtype(dtype).name, 1)


def f32_bits(value) -> int:
    """IEEE-754 bit pattern.  Comparisons run on these, never on decimals."""
    return struct.unpack("<I", struct.pack("<f", np.float32(value)))[0]


def bits_f32(pattern: int) -> np.float32:
    return np.float32(struct.unpack("<f", struct.pack("<I", int(pattern)))[0])


def binary_sum(values, dtype=np.float32) -> np.floating:
    """``BinarySum<T, N>`` (``helpers/utils/utils.hpp:39-54``) -- divide and conquer.

    ``sum(x) = sum(x[:N/2]) + sum(x[N/2:])``.  Requires a power-of-two length, which the callers
    guarantee: beats are ``2^logParEntries`` wide and chunks are ``2^logDelays``.
    """
    dt = np.dtype(dtype).type
    v = [dt(x) for x in values]
    if len(v) & (len(v) - 1):
        raise ValueError(f"BinarySum needs a power-of-two length, got {len(v)}")
    while len(v) > 1:
        v = [dt(v[i] + v[i + 1]) for i in range(0, len(v), 2)]
    return v[0]


def dot(a, b, par_entries: int = 4, delays: int | None = None,
        dtype=np.float32) -> np.floating:
    """One row of ``gemv`` -- ``dot_tree``, the reduction ``float`` and ``double`` take.

    ``par_entries`` is ``1 << t_LogParEntries``, the stream width; ``delays`` defaults to the
    element type's ``AdderDelay`` (**4 for float, 8 for double**).  Both change the result, so
    both are explicit.

    ``dtype`` selects the element type.  It is a real parameter, not decoration: ``double`` takes
    the same code path but groups beats by 8 instead of 4, and every rounding happens at 53 bits
    instead of 24.  An earlier version hardcoded ``float32`` while ``adder_delays`` already
    answered for ``float64``, so a caller asking for double got plausible, silently wrong
    numbers -- exactly the failure this project exists to prevent.
    """
    dt = np.dtype(dtype)
    if dt.name not in ADDER_DELAYS:
        raise NotImplementedError(
            f"dot_tree is the float/double path; {dt.name} takes dot_dsp -- use dot_int(). "
            f"Supported here: {sorted(ADDER_DELAYS)}.")
    t = dt.type
    a = np.asarray(a, dtype=dt)
    b = np.asarray(b, dtype=dt)
    if a.shape != b.shape:
        raise ValueError(f"length mismatch: {a.shape} vs {b.shape}")
    if a.size % par_entries:
        raise ValueError(f"n={a.size} must be a multiple of parEntries={par_entries}")
    d = adder_delays(dt) if delays is None else delays

    products = [t(a[i] * b[i]) for i in range(a.size)]
    beats = [binary_sum(products[s:s + par_entries], dt)
             for s in range(0, len(products), par_entries)]
    # padding(): the beat count is padded up to a multiple of Delays.  Padding with zero is exact
    # in IEEE-754 for every finite value, so it moves no bits -- but it does decide the chunking,
    # which does.
    while len(beats) % d:
        beats.append(t(0))
    total = t(0)
    for s in range(0, len(beats), d):
        total = t(total + binary_sum(beats[s:s + d], dt))
    return total


def gemv(matrix, vector, par_entries: int = 4, delays: int | None = None,
         dtype=np.float32) -> np.ndarray:
    """``y = M x``, bit-exact against ``xf::blas::gemv`` for ``float`` and ``double``.

    The library streams ``x`` once per row (its testbench uses ``vec2GemStream``), so every row
    sees the same vector and the rows are independent.
    """
    dt = np.dtype(dtype)
    m = np.asarray(matrix, dtype=dt)
    v = np.asarray(vector, dtype=dt)
    if m.ndim != 2 or m.shape[1] != v.size:
        raise ValueError(f"shape mismatch: matrix {m.shape}, vector {v.shape}")
    return np.array([dot(m[r], v, par_entries, delays, dt) for r in range(m.shape[0])],
                    dtype=dt)


# --- the non-float path -----------------------------------------------------------------------
#: Element widths ``dot_dsp`` is exercised with.  ``dot_tree`` handles float and double; every
#: other type takes this path.
INT_WIDTHS = {"int16": 16, "int32": 32}


def dot_int(a, b, width: int = 32, signed: bool = True) -> int:
    """``dot_dsp`` (``helpers/funcs/dotHelper.hpp:75-100``) -- the non-float reduction.

    Nothing tree-shaped here::

        t_MacDataType l_res = 0;
        for each beat:  for j in 0..parEntries-1:  l_res += l_x[j] * l_y[j];

    A single accumulator, updated in index order.  So ``parEntries`` does **not** change the
    result on this path -- unlike the float path, where it is part of the numerical contract.
    The risk moves from summation order to the accumulator's **width**: it is ``t_MacDataType``,
    which in practice equals the element type (see below), so a long dot product wraps.

    ``t_MacDataType`` cannot be widened.  ``gemv`` declares its output stream as
    ``WideType<t_DataType, 1>`` while forwarding to a ``DotHelper`` parameterised on
    ``t_MacDataType``, so any differing MAC type fails to compile inside ``gemv.hpp:47``.  The
    parameter is exposed, documented, and dead -- in 2023.1 and 2025.1 alike.  Hence a single
    ``width`` here rather than separate element and accumulator widths.

    ``signed`` selects how the wrapped accumulator is read back.  The stored bits are the same
    either way -- ``ap_uint<32>`` and ``int32_t`` produce identical hardware -- but the value they
    denote is not, and an unsigned kernel returns the large positive number where a signed one
    returns its negative counterpart.  Measured against both.
    """
    a = np.asarray(a, dtype=object)
    b = np.asarray(b, dtype=object)
    if a.shape != b.shape:
        raise ValueError(f"length mismatch: {a.shape} vs {b.shape}")
    mask = (1 << width) - 1
    acc = 0
    for i in range(a.size):
        acc = (acc + int(a[i]) * int(b[i])) & mask      # wraps at the accumulator width
    if signed and acc >> (width - 1):
        return acc - (1 << width)
    return acc


def gemv_int(matrix, vector, width: int = 32, signed: bool = True) -> np.ndarray:
    """``y = M x`` on the ``dot_dsp`` path, with the accumulator wrapping at ``width``.

    ``dtype=object`` rather than ``int64`` so the accumulate is exact at any width and numpy
    never has to overflow.  At ``width = 64`` this is a matter of hygiene, not of the answer:
    masking is a ring homomorphism, so truncating each product first gives the same result --
    measured, 0 of 15 int64 golden rows differ.  It stops mattering only if the model is ever
    asked for a width numpy cannot hold at all.
    """
    m = np.asarray(matrix, dtype=object)
    v = np.asarray(vector, dtype=object)
    if m.ndim != 2 or m.shape[1] != v.size:
        raise ValueError(f"shape mismatch: matrix {m.shape}, vector {v.shape}")
    return np.array([dot_int(m[r], v, width, signed) for r in range(m.shape[0])], dtype=object)


# --- the alpha/beta overload ------------------------------------------------------------------
def scal(vector, alpha) -> np.ndarray:
    """``scal`` (``scal.hpp:66``) -- ``alpha * x``, elementwise, rounded to float32 per element."""
    v = np.asarray(vector, dtype=np.float32)
    a = np.float32(alpha)
    return np.array([np.float32(a * v[i]) for i in range(v.size)], dtype=np.float32)


def axpy(x, y, alpha) -> np.ndarray:
    """``axpy`` (``axpy.hpp:71``) -- ``alpha * x + y``, as **two** rounded operations.

    The library writes it as one expression, ``p_alpha * l_realX + l_realY``, which is a fused
    multiply-add candidate.  An FMA keeps the product's full precision and rounds once; a separate
    multiply and add round twice.  **They give different bits** -- measured, 25 of 576 rows on the
    S5 golden.  This models the unfused reading, and ``tests/test_gemv_ab.py`` pins that choice
    against the library rather than leaving it to the compiler's mood.
    """
    xf = np.asarray(x, dtype=np.float32)
    yf = np.asarray(y, dtype=np.float32)
    a = np.float32(alpha)
    return np.array([np.float32(np.float32(a * xf[i]) + yf[i]) for i in range(xf.size)],
                    dtype=np.float32)


def gemv_ab(matrix, vector, y, alpha, beta, par_entries: int = 4,
            delays: int | None = None) -> np.ndarray:
    """``yr = alpha * (M x) + beta * y`` -- the 8-arg overload (``gemv.hpp:66-85``).

    No new arithmetic: it is the 5-arg ``gemv``, then ``scal``, then ``axpy``.  What matters is
    that ``beta * y`` is rounded to float32 in ``scal`` **before** ``axpy`` adds it -- computing
    ``alpha*dot + beta*y`` in one wider expression and rounding once gives different bits.

    Unlike the 5-arg overload this one takes no ``t_MacDataType``, so the accumulator is always
    the element type.
    """
    dot_result = gemv(matrix, vector, par_entries, delays)
    return axpy(dot_result, scal(y, beta), alpha)
