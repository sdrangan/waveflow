"""Bit-exact model of the Vitis L1 SSR FFT twiddle table.

The shipped generator is ``xf::dsp::fft::TwiddleTable::initTwiddleTable``
(``hls_ssr_fft_twiddle_table.hpp:62-70``)::

    typedef typename TwiddleTypeCastingTraits<...>::T_roundingBasedCastType casting_type;
    for (int i = 0; i < EXT_LEN; i++) {
        double real =  cos((2 * i * M_PI) / t_L);
        double imag = -sin((2 * i * M_PI) / t_L);
        p_table[i] = casting_type(real, imag);
    }

Two things that are easy to get wrong, and the reason S1 exists:

* The cast is the **rounding** one -- ``ap_fixed<W, I, AP_RND, AP_SAT>``.  The truncation
  typedef sitting beside it in the traits header is declared four times and never used, so a
  model built on ``AP_TRN``/``AP_WRAP`` -- ``ap_fixed``'s defaults, and the obvious guess -- is
  wrong at 10 of 16 entries for L=16.
* ``imag`` carries a **leading minus** (the forward transform's ``e^{-j2pi i/L}``), applied in
  ``double`` *before* quantization.

Built from the same primitives as the rest of Waveflow's fixed-point models
(``examples/schemas/complex`` is the reference): ``fixputils.quantize_real`` to load reals,
``complexutils.make_complex`` to assemble, ``DataArray[ComplexField]`` as the carrier, and
``fixputils.to_bits`` for the stored-bit view.  Nothing here reimplements quantization.
"""
from __future__ import annotations

import numpy as np

from waveflow.hw.complexfield import ComplexField
from waveflow.hw.dataschema import DataArray
from waveflow.hw.fixpoint import FixedField
from waveflow.utils import complexutils as cx
from waveflow.utils import fixputils
from waveflow.utils.fixputils import OMode, QMode

#: The default parameter struct's twiddle format (``hls_ssr_fft_enums.hpp:82-83``).
#: ``I = 2`` is load-bearing: it is what lets +1/-1 store exactly.
TWIDDLE_W = 18
TWIDDLE_I = 2


def twiddle_type(w: int = TWIDDLE_W, i: int = TWIDDLE_I) -> type[FixedField]:
    """The ``FixedField`` matching ``T_roundingBasedCastType`` for ``ap_fixed<w, i>``.

    ``AP_RND`` here is round-half-**up** (toward +inf), matching ``quantize_real``:
    ``-0.5 lsb`` quantizes to ``0``, not to ``-1``.
    """
    return FixedField.specialize(w, i, True, QMode.AP_RND, OMode.AP_SAT)


def twiddle_complex_type(w: int = TWIDDLE_W, i: int = TWIDDLE_I) -> type[ComplexField]:
    """``std::complex<ap_fixed<w, i, AP_RND, AP_SAT>>`` as a ``ComplexField``."""
    return ComplexField.specialize(twiddle_type(w, i))


def ext_len(length: int, radix: int) -> int:
    """``TwiddleTableLENTraits<L, R>::EXTENDED_TWIDDLE_TALBE_LENGTH``.

    Confirmed = ``L`` for the S1 case (L=16, R=4) by dumping it from the C++ side.  The general
    formula is not modelled, so anything else raises rather than guessing.
    """
    if length == 16 and radix == 4:
        return 16
    raise NotImplementedError(
        f"ext_len not yet modelled for L={length}, R={radix} -- dump it from "
        "cpp/dump_twiddle.cpp and extend this function with the value it reports.")


def twiddle_ideal(length: int, n: int) -> np.ndarray:
    """The unquantized twiddles in ``float64`` -- exactly the C++ ``double`` expressions."""
    i = np.arange(n, dtype=np.float64)
    return np.cos(2.0 * i * np.pi / length) - 1j * np.sin(2.0 * i * np.pi / length)


def twiddle_table(length: int, radix: int, w: int = TWIDDLE_W, i: int = TWIDDLE_I,
                  n: int | None = None) -> DataArray:
    """The quantized table as a ``DataArray[ComplexField]`` -- the form S2's ``cmult`` consumes."""
    count = ext_len(length, radix) if n is None else n
    ideal = twiddle_ideal(length, count)
    fmt = twiddle_type(w, i).get_format()
    re = fixputils.quantize_real(np.real(ideal), fmt)
    im = fixputils.quantize_real(np.imag(ideal), fmt)
    cf = twiddle_complex_type(w, i)
    return DataArray.specialize(cf, max_shape=(count,))(cx.make_complex(re, im, fmt))


def twiddle_bits(length: int, radix: int, w: int = TWIDDLE_W, i: int = TWIDDLE_I,
                 n: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """``(re, im)`` as raw ``w``-bit unsigned patterns -- what ``ap_fixed::range()`` yields.

    The golden comparison runs on these, never on floats: a ``double`` round-trip can absorb
    exactly the 1-LSB error this stage exists to catch.
    """
    da = twiddle_table(length, radix, w, i, n)
    v = np.asarray(da.val)
    return (np.asarray(fixputils.to_bits(cx.re_of(v), w)),
            np.asarray(fixputils.to_bits(cx.im_of(v), w)))
