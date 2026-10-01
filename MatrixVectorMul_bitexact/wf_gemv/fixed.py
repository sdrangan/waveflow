"""Bit-exact model of ``xf::blas::gemv`` for an ``ap_fixed`` element type -- S4.

``gemv`` is a template, so ``ap_fixed`` instantiates and compiles.  It also **returns the wrong
answer**, and this module models both what the library emits today and what it would emit with
the one-line defect fixed.  Both are validated against goldens produced by the library's own code
(the second against a copy of one header carrying that one change -- see
``cpp/vendor_patched/dotHelper_patched.hpp``).

## Where the arithmetic lives

``ap_fixed`` is not ``float``, so ``DotHelper`` dispatches to ``dot_dsp``
(``helpers/funcs/dotHelper.hpp:75-100``), the same single-accumulator path as the integer case::

    t_MacDataType l_res = 0;
    for each beat:  for j in 0..parEntries-1:  l_res += l_x[j] * l_y[j];

The accumulator is ``t_MacDataType``, which cannot differ from ``t_DataType`` (S3: the parameter
is exposed but uncompilable), so it accumulates **in the element format**.  Per element::

    product        exact -- ap_fixed<W,I> * ap_fixed<W,I> is ap_fixed<2W,2I>, no loss
    l_res + product  exact -- the operator's return type is wide enough
    assignment       LOSSY -- narrowing to <W,I> applies the format's Q and O modes

So the interesting knobs move from summation order (the float path) to **Q and O**, applied once
per element rather than once at the end.  ``parEntries`` still does not change the result -- the
loop order is index order at any stream width -- and a test measures that rather than assuming it.

## ⚠️ The defect: ``gemv`` returns garbage for ``ap_fixed``

``dot_dsp`` ends with (``dotHelper.hpp:98``)::

    p_res.write(l_res);          // l_res is t_MacDataType; the stream carries ap_uint<W>

That conversion is **numeric, not a bit repack**.  For an integer type it is the identity, which
is why S3 never saw it.  For ``ap_fixed`` it truncates toward zero to the integer part, and the
consumer then unpacks that integer as a raw stored field -- so the value comes back divided by
``2**F`` with every fractional bit gone::

    ap_fixed<16,8>, true dot = 27.75  ->  stream carries 27  ->  reads back as 27/256 = 0.105469

The float path does not have this: ``postProcess`` (``sum.hpp:79``) writes a ``WideType``, whose
``operator t_TypeInt`` packs by ``reinterpret_cast``.  Two sibling functions, two conventions.

``dotHelper.hpp`` is **byte-identical between 2023.1 and 2025.1**, so this is long-standing.
:func:`dot_fixed_as_shipped` reproduces it exactly; :func:`dot_fixed` is what the same kernel
computes internally and emits once the write is corrected.

## Limits

* Signed ``ap_fixed`` only; ``ap_ufixed`` is not modelled.
* ``W <= 31``.  The exact intermediate ``l_res + product`` needs ``2W+1`` bits and
  ``waveflow.utils.fixputils`` is int64-backed, so it refuses wider formats rather than wrapping
  silently.  A limit of the numeric core, not of the approach.
"""
from __future__ import annotations

import numpy as np

from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format, OMode, QMode

__all__ = [
    "Format",
    "OMode",
    "QMode",
    "as_shipped",
    "dot_fixed",
    "dot_fixed_as_shipped",
    "fixed_format",
    "gemv_fixed",
    "gemv_fixed_as_shipped",
]


def fixed_format(w: int, int_bits: int, q_mode: QMode = QMode.AP_TRN,
                 o_mode: OMode = OMode.AP_WRAP) -> Format:
    """``ap_fixed<w, int_bits, q_mode, o_mode>`` as a :class:`fixputils.Format`.

    Rejects the widths the model cannot carry, at construction rather than at the first wrong
    answer: the exact ``acc + product`` intermediate is ``2w+1`` bits.
    """
    if 2 * w + 1 > fx.MAX_WIDTH:
        raise NotImplementedError(
            f"W={w} needs a {2 * w + 1}-bit exact accumulate intermediate, over fixputils' "
            f"{fx.MAX_WIDTH}-bit ceiling; W <= {(fx.MAX_WIDTH - 1) // 2} is supported.")
    return Format(w, int_bits, signed=True, q_mode=q_mode, o_mode=o_mode)


def dot_fixed(a_stored, b_stored, fmt: Format) -> int:
    """One row of ``dot_dsp`` in ``fmt``, returned as the stored integer.

    Inputs are **stored integers** (the ``.range()`` field), never floats: a float input would
    add a quantization step the library does not perform and put a rounding question between the
    test data and the arithmetic under test.
    """
    a = np.asarray(a_stored, dtype=np.int64)
    b = np.asarray(b_stored, dtype=np.int64)
    if a.shape != b.shape:
        raise ValueError(f"length mismatch: {a.shape} vs {b.shape}")

    prod, prod_fmt = fx.mult(a, fmt, b, fmt)            # exact: <2W, 2I>
    acc = np.int64(0)
    for i in range(a.size):
        wide, wide_fmt = fx.add(acc, fmt, prod[i], prod_fmt)   # exact
        acc = np.int64(fx.quantize(wide, wide_fmt, fmt))       # the lossy step: Q then O
    return int(acc)


def gemv_fixed(matrix_stored, vector_stored, fmt: Format) -> np.ndarray:
    """``y = M x`` in ``fmt``, as stored integers -- the corrected library's output."""
    m = np.asarray(matrix_stored, dtype=np.int64)
    v = np.asarray(vector_stored, dtype=np.int64)
    if m.ndim != 2 or m.shape[1] != v.size:
        raise ValueError(f"shape mismatch: matrix {m.shape}, vector {v.shape}")
    return np.array([dot_fixed(m[r], v, fmt) for r in range(m.shape[0])], dtype=np.int64)


def as_shipped(acc_stored: int, fmt: Format) -> int:
    """The defect at ``dotHelper.hpp:98``: ``ap_fixed`` -> ``ap_uint<W>`` by **value**.

    Truncation toward zero to the integer part, then two's complement into ``W`` bits.  The
    consumer unpacks that as a stored field, so the returned integer is again a stored value in
    ``fmt`` -- just not the one the kernel computed.  Verified against the library for positive
    and negative results, and for both overflow modes.
    """
    s = int(acc_stored)
    f = fmt.frac_bits
    trunc = s >> f if s >= 0 else -((-s) >> f)          # toward zero, not floor
    bits = trunc & ((1 << fmt.W) - 1)
    return bits - (1 << fmt.W) if bits >> (fmt.W - 1) else bits


def dot_fixed_as_shipped(a_stored, b_stored, fmt: Format) -> int:
    """What the shipped library actually emits for one row."""
    return as_shipped(dot_fixed(a_stored, b_stored, fmt), fmt)


def gemv_fixed_as_shipped(matrix_stored, vector_stored, fmt: Format) -> np.ndarray:
    """``y = M x`` as the shipped library emits it -- fractional bits gone.  See the defect note."""
    return np.array([as_shipped(v, fmt)
                     for v in gemv_fixed(matrix_stored, vector_stored, fmt)], dtype=np.int64)
