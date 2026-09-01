"""Bit-exact model of the Vitis L1 SSR FFT twiddle table.

The shipped generator is ``xf::dsp::fft::TwiddleTable::initTwiddleTable``
(``hls_ssr_fft_twiddle_table.hpp:62-70``)::

    typedef typename TwiddleTypeCastingTraits<...>::T_roundingBasedCastType casting_type;
    for (int i = 0; i < EXT_LEN; i++) {
        double real =  cos((2 * i * M_PI) / t_L);
        double imag = -sin((2 * i * M_PI) / t_L);
        p_table[i] = casting_type(real, imag);
    }

Two things that are easy to get wrong and are the whole point of S1:

* The cast is the **rounding** one -- ``ap_fixed<W, I, AP_RND, AP_SAT>``.  The truncation
  typedef sitting beside it in the traits header is declared four times and never used, so a
  model built on ``AP_TRN``/``AP_WRAP`` is wrong even though those are ``ap_fixed``'s defaults.
* ``imag`` carries a **leading minus** (the forward transform's ``e^{-j2pi i/L}``), applied in
  ``double`` *before* quantization.  Negating after quantizing is not the same operation under
  ``AP_RND``, which rounds half away from zero.
"""
from __future__ import annotations

import numpy as np

from waveflow.hw.fixpoint import FixedField
from waveflow.utils.fixputils import OMode, QMode

#: The default parameter struct's twiddle format (``hls_ssr_fft_enums.hpp:82-83``).
#: ``I = 2`` is deliberate: it is what lets +1/-1 store exactly.
TWIDDLE_W = 18
TWIDDLE_I = 2


def twiddle_type(w: int = TWIDDLE_W, i: int = TWIDDLE_I) -> type[FixedField]:
    """The ``FixedField`` matching ``T_roundingBasedCastType`` for ``ap_fixed<w, i>``."""
    return FixedField.specialize(w, i, True, QMode.AP_RND, OMode.AP_SAT)


def ext_len(length: int, radix: int) -> int:
    """``TwiddleTableLENTraits<L, R>::EXTENDED_TWIDDLE_TALBE_LENGTH``.

    Confirmed = ``L`` for the S1 case (L=16, R=4) by dumping it from the C++ side; the general
    formula is not yet modelled, so callers outside that case must pass their own length.
    """
    if length == 16 and radix == 4:
        return 16
    raise NotImplementedError(
        f"ext_len not yet modelled for L={length}, R={radix} -- dump it from "
        "cpp/dump_twiddle.cpp and extend this function with the value it reports.")


def twiddle_ideal(length: int, n: int) -> np.ndarray:
    """The unquantized twiddles, in ``float64`` -- exactly the C++ ``double`` expressions."""
    i = np.arange(n, dtype=np.float64)
    return np.cos(2.0 * i * np.pi / length) - 1j * np.sin(2.0 * i * np.pi / length)


def twiddle_stored(length: int, radix: int, w: int = TWIDDLE_W, i: int = TWIDDLE_I,
                   n: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Quantized twiddle table as **raw stored integers**, matching the C++ dump.

    Returns ``(re, im)`` as unsigned ``w``-bit two's-complement patterns -- the same
    representation ``ap_fixed::range().to_int64()`` yields, so the comparison is on bits and
    never round-trips through a float.
    """
    count = ext_len(length, radix) if n is None else n
    ideal = twiddle_ideal(length, count)
    ft = twiddle_type(w, i)
    re = _quantize_to_stored(np.real(ideal), ft)
    im = _quantize_to_stored(np.imag(ideal), ft)
    return re, im


def _quantize_to_stored(values: np.ndarray, ft: type[FixedField]) -> np.ndarray:
    """Real ``float64`` -> ``ft`` -> unsigned stored-bit pattern."""
    from waveflow.hw.dataschema import DataArray
    from waveflow.hw import fixpoint as fx

    arr = DataArray.specialize(ft, max_shape=(values.size,))
    da = fx.from_real(values, ft)
    stored = np.asarray(da.val if hasattr(da, "val") else da).astype(np.int64).reshape(-1)
    return stored & ((1 << ft.get_format().W) - 1)
