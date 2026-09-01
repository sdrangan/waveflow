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


def binary_sum(values) -> np.float32:
    """``BinarySum<T, N>`` (``helpers/utils/utils.hpp:39-54``) -- divide and conquer.

    ``sum(x) = sum(x[:N/2]) + sum(x[N/2:])``.  Requires a power-of-two length, which the callers
    guarantee: beats are ``2^logParEntries`` wide and chunks are ``2^logDelays``.
    """
    v = [np.float32(x) for x in values]
    if len(v) & (len(v) - 1):
        raise ValueError(f"BinarySum needs a power-of-two length, got {len(v)}")
    while len(v) > 1:
        v = [np.float32(v[i] + v[i + 1]) for i in range(0, len(v), 2)]
    return v[0]


def dot(a, b, par_entries: int = 4, delays: int | None = None) -> np.float32:
    """One row of ``gemv`` -- ``dot_tree`` for a float element type.

    ``par_entries`` is ``1 << t_LogParEntries``, the stream width; ``delays`` defaults to the
    element type's ``AdderDelay``.  Both change the result, so both are explicit.
    """
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    if a.shape != b.shape:
        raise ValueError(f"length mismatch: {a.shape} vs {b.shape}")
    if a.size % par_entries:
        raise ValueError(f"n={a.size} must be a multiple of parEntries={par_entries}")
    d = adder_delays("float32") if delays is None else delays

    products = [np.float32(a[i] * b[i]) for i in range(a.size)]
    beats = [binary_sum(products[s:s + par_entries])
             for s in range(0, len(products), par_entries)]
    # padding(): the beat count is padded up to a multiple of Delays.  Padding with zero is exact
    # in IEEE-754 for every finite value, so it moves no bits -- but it does decide the chunking,
    # which does.
    while len(beats) % d:
        beats.append(np.float32(0))
    total = np.float32(0)
    for s in range(0, len(beats), d):
        total = np.float32(total + binary_sum(beats[s:s + d]))
    return total


def gemv(matrix, vector, par_entries: int = 4, delays: int | None = None) -> np.ndarray:
    """``y = M x``, bit-exact against ``xf::blas::gemv`` for ``float``.

    The library streams ``x`` once per row (its testbench uses ``vec2GemStream``), so every row
    sees the same vector and the rows are independent.
    """
    m = np.asarray(matrix, dtype=np.float32)
    v = np.asarray(vector, dtype=np.float32)
    if m.ndim != 2 or m.shape[1] != v.size:
        raise ValueError(f"shape mismatch: matrix {m.shape}, vector {v.shape}")
    return np.array([dot(m[r], v, par_entries, delays) for r in range(m.shape[0])],
                    dtype=np.float32)


# --- the non-float path -----------------------------------------------------------------------
#: Element widths ``dot_dsp`` is exercised with.  ``dot_tree`` handles float and double; every
#: other type takes this path.
INT_WIDTHS = {"int16": 16, "int32": 32}


def dot_int(a, b, width: int = 32) -> int:
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
    """
    a = np.asarray(a, dtype=np.int64)
    b = np.asarray(b, dtype=np.int64)
    if a.shape != b.shape:
        raise ValueError(f"length mismatch: {a.shape} vs {b.shape}")
    mask = (1 << width) - 1
    sign = 1 << (width - 1)
    acc = 0
    for i in range(a.size):
        acc = (acc + int(a[i]) * int(b[i])) & mask      # wraps at the accumulator width
    return acc - (1 << width) if acc & sign else acc


def gemv_int(matrix, vector, width: int = 32) -> np.ndarray:
    """``y = M x`` on the ``dot_dsp`` path, with the accumulator wrapping at ``width``."""
    m = np.asarray(matrix, dtype=np.int64)
    v = np.asarray(vector, dtype=np.int64)
    if m.ndim != 2 or m.shape[1] != v.size:
        raise ValueError(f"shape mismatch: matrix {m.shape}, vector {v.shape}")
    return np.array([dot_int(m[r], v, width) for r in range(m.shape[0])], dtype=np.int64)
