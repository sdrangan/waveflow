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

    Deprecated alias for :func:`quarter_table_len`, kept because the S1 tests and docs name it.
    It once raised for anything but ``L=16``; that was superseded when the quarter-wave path was
    measured at 16, 64 and 1024, and leaving two functions answering the same question -- one of
    them wrong by omission -- was the actual hazard.
    """
    return quarter_table_len(length, radix)


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


# --- quarter-wave table access ----------------------------------------------------------------
def quarter_table_len(length: int, radix: int) -> int:
    """``TwiddleTableLENTraits<L,R>::EXTENDED_TWIDDLE_TALBE_LENGTH`` -- measured, not derived.

    ``L=16 -> 16`` (the whole circle), ``L=64 -> 16``, ``L=1024 -> 256`` (i.e. ``L/4``).  So for
    anything past ``L=16`` the stored table holds only a quarter wave and the rest of the circle
    is *reconstructed* -- see :func:`read_quarter_twiddle`.
    """
    return length if length <= 16 else length // 4


def read_quarter_twiddle(index: int, tbl_im: np.ndarray, length: int,
                         w: int = TWIDDLE_W, i: int = TWIDDLE_I) -> tuple[int, int]:
    """``readQuaterTwiddleTable`` (``hls_ssr_fft_twiddle_table.hpp:103-148``).

    Both the real and imaginary parts are read from the table's **imaginary** column (the
    ``-sin`` quarter wave); the real part just enters at a ``+3L/4`` phase offset.  Everything
    else is index symmetry:

    * bit ``phase-2`` of the index inverts the LUT index (two's complement within its width)
    * bit ``phase-1`` negates the output
    * the two axis indices ``L/4`` and ``3L/4`` bypass the table entirely and return ``-1.0``

    That last case is why this cannot be replaced by quantizing ``cos``/``sin`` directly: at the
    axis points the hardware substitutes an exact ``-1``, and elsewhere it reuses one quarter of
    the wave, so a value read here can differ by an LSB from an independently quantized one.
    Returns ``(re, im)`` as *signed* stored integers.
    """
    phase = int(np.log2(length))
    mask = (1 << phase) - 1
    lut_w = phase - 2
    lut_mask = (1 << lut_w) - 1
    minus_one = -(1 << (w - i))                 # -1.0 in ap_fixed<w,i>
    fmt = twiddle_type(w, i).get_format()

    def path(idx: int) -> int:
        idx &= mask
        invert = (idx >> (phase - 2)) & 1
        negate = (idx >> (phase - 1)) & 1
        saturate = idx in (length // 4, 3 * length // 4)
        lut_index = idx & lut_mask
        if invert:
            lut_index = (-lut_index) & lut_mask
        temp = minus_one if saturate else int(tbl_im[lut_index])
        if negate:
            temp = int(fixputils._apply_overflow(np.array([-temp]), fmt)[0])
        return temp

    return path(index + 3 * length // 4), path(index)


def quarter_twiddles(length: int, w: int = TWIDDLE_W, i: int = TWIDDLE_I
                     ) -> tuple[np.ndarray, np.ndarray]:
    """The full circle of twiddles as the hardware sees it -- via the quarter-wave path."""
    ext = quarter_table_len(length, 4)
    ideal = twiddle_ideal(length, ext)
    fmt = twiddle_type(w, i).get_format()
    tbl_im = fixputils.quantize_real(np.imag(ideal), fmt)
    re = np.zeros(length, dtype=np.int64)
    im = np.zeros(length, dtype=np.int64)
    for n in range(length):
        re[n], im[n] = read_quarter_twiddle(n, tbl_im, length, w, i)
    return re, im
