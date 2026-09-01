"""The two complex primitives the FFT butterfly is built from.

``cquantize`` now lives in ``waveflow.hw.complexfield`` (promoted in S3, once the complex
conformance harness proved it against Vitis); what remains here is a thin shim plus the
FFT-specific ``complex_multiply``, which deliberately did NOT move -- it encodes this design's
quantization points, not complex arithmetic in general.

``waveflow/hw/complexfield.py`` exports ``cadd / csub / cmult / conj / csum`` and nothing lossy.
The FFT needs lossy: ``FFTMultiplicationTraits`` (``hls_ssr_fft_multiplication_traits.hpp:67-74``)
requantizes every twiddle product into ``ap_fixed<product_WL, product_IL, AP_TRN, AP_WRAP>``,
which is far narrower than the full-precision ``(2W+1, 2I+1)`` ``cmult`` returns.

**This adds no quantization logic.**  It splits re/im, calls ``fixputils.quantize`` -- the same
function ``fixpoint.quantize`` uses, already conformance-tested against real Vitis kernels
across all four ``QMode`` x ``OMode`` combinations by ``examples/schemas/fixedpoint`` -- and
recombines.  The shape is exactly ``complexfield.csum``'s.

Kept local to ``fft_bitexact/`` deliberately: promoting it into ``waveflow/hw/complexfield.py``
should happen once S2 is bit-exact and the semantics are proven, not before.
"""
from __future__ import annotations

import numpy as np

from waveflow.hw.complexfield import ComplexField
from waveflow.hw.complexfield import cquantize as _lib_cquantize
from waveflow.hw.dataschema import DataArray
from waveflow.hw.fixpoint import FixedField
from waveflow.utils import fixputils
from waveflow.utils.fixputils import Format


def product_format(op1: Format, op2: Format) -> Format:
    """``FFTMultiplicationTraits`` (``hls_ssr_fft_multiplication_traits.hpp:67-74``)::

        product_IL = max(op1_IL, op2_IL)
        product_FL = max(op1_FL, op2_FL)
        product_WL = product_IL + product_FL
        ap_fixed<product_WL, product_IL, AP_TRN, AP_WRAP, 0>

    Note this is **max**, not sum: the product is deliberately not full precision.  The modes
    are fixed by the library -- truncate and wrap -- and differ from the twiddle *table*'s
    ``AP_RND``/``AP_SAT``.  Getting those two backwards is a 1-LSB error.
    """
    from waveflow.utils.fixputils import OMode, QMode
    product_il = max(op1.int_bits, op2.int_bits)
    product_fl = max(op1.W - op1.int_bits, op2.W - op2.int_bits)
    return Format(W=product_il + product_fl, int_bits=product_il, signed=True,
                  q_mode=QMode.AP_TRN, o_mode=OMode.AP_WRAP)


def fixed_from_format(fmt: Format) -> type[FixedField]:
    return FixedField.specialize(fmt.W, fmt.int_bits, fmt.signed, fmt.q_mode, fmt.o_mode)


def complex_from_format(fmt: Format) -> type[ComplexField]:
    return ComplexField.specialize(fixed_from_format(fmt))


def cquantize(a: DataArray, target: Format | type[ComplexField]) -> DataArray:
    """Requantize a ``DataArray[ComplexField]`` -- now a thin shim over the library.

    Promoted to :func:`waveflow.hw.complexfield.cquantize` in S3, after the complex conformance
    harness proved it bit-exact against Vitis over both ``QMode`` x both ``OMode``.  Kept here
    only to accept a bare ``Format`` (convenient for the FFT's derived per-stage formats); the
    arithmetic is the library's.
    """
    tgt = complex_from_format(target) if isinstance(target, Format) else target
    return _lib_cquantize(a, tgt)


def complex_multiply(a_re: np.ndarray, a_im: np.ndarray, op1: Format,
                     b_re: np.ndarray, b_im: np.ndarray, op2: Format,
                     prod: Format) -> tuple[np.ndarray, np.ndarray]:
    """The library's ``complexMultiply`` (``hls_ssr_fft_complex_multiplier.hpp:29-45``).

    **Not** a full-precision complex product followed by one requantize.  The C++ stores every
    partial product into ``T_op1`` -- the *first operand's* type -- before combining::

        T_op1 real1 = op1.real() * op2.real();   // truncated into T_op1 here
        T_op1 real2 = op1.imag() * op2.imag();   // and here
        T_op1 real_out = real1 - real2;          // subtracted in T_op1
        p_product.real(real_out);                // then widened into T_prd

    So there are three quantization points per component, not one.  Measured against the C++
    golden over 24 cases: this model is exact (0 wrong); "full product then one requantize" is
    wrong in 18/24 real and 23/24 imaginary parts.  Composing ``complexfield.cmult`` with a
    single ``cquantize`` is therefore the wrong recipe for this FFT, however natural it looks.

    Operates on stored integers; returns ``(re, im)`` stored in ``prod``.
    """
    def to_op1(stored: np.ndarray, fmt: Format) -> np.ndarray:
        return fixputils.quantize(stored, fmt, op1)

    p_rr, f_m = fixputils.mult(a_re, op1, b_re, op2)
    p_ii, _ = fixputils.mult(a_im, op1, b_im, op2)
    real1, real2 = to_op1(p_rr, f_m), to_op1(p_ii, f_m)
    real_sum, f_s = fixputils.sub(real1, op1, real2, op1)
    out_re = fixputils.quantize(to_op1(real_sum, f_s), op1, prod)

    p_ri, _ = fixputils.mult(a_re, op1, b_im, op2)
    p_ir, _ = fixputils.mult(a_im, op1, b_re, op2)
    imag1, imag2 = to_op1(p_ri, f_m), to_op1(p_ir, f_m)
    imag_sum, f_a = fixputils.add(imag1, op1, imag2, op1)
    out_im = fixputils.quantize(to_op1(imag_sum, f_a), op1, prod)

    return out_re, out_im
