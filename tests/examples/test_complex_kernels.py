"""Structure tests for the migrated ComplexField conformance kernel renderer.

The kernel is now the thin C-sim wrapper around the **generated serialization** (the
``<type>_array_utils::read_array_slice`` / ``write_array_slice`` from the ComplexField C++ codegen)
and **``complex_utils.hpp``** arithmetic -- it no longer hand-rolls interleaving or the inline
complex formula.  These tests lock that contract: the right includes, the
``complex_utils::`` op calls, the generated ``read_array_slice`` / ``write_array_slice`` with the
per-type word widths, and that round-trip is the identity / conj ignores ``in_b``.

The end-to-end **bit-exact** Vitis conformance lives in ``test_complex_conformance.py``.
"""
from examples.schemas.complex.kernels import render_kernel


def _k(op, *, in_cpp="std::complex<ap_fixed<8, 4, AP_TRN, AP_WRAP>>",
       out_cpp="std::complex<ap_fixed<17, 9, AP_TRN, AP_WRAP>>",
       in_ns="complex__fixed8_4_array_utils", out_ns="complex__fixed17_9_array_utils",
       in_hdr="complex__fixed8_4_array_utils.h", out_hdr="complex__fixed17_9_array_utils.h",
       wbi=16, wbo=34, n=5, nwa=5, nwy=5, binary=True):
    return render_kernel(op, in_cpp, out_cpp, in_ns, out_ns, in_hdr, out_hdr,
                         wbi, wbo, n, nwa, nwy, binary)


def test_kernel_uses_complex_utils_and_generated_serialization():
    k = _k("cmult")
    assert '#include "complex_utils.hpp"' in k
    assert '#include "complex__fixed8_4_array_utils.h"' in k          # input serialization
    assert '#include "complex__fixed17_9_array_utils.h"' in k         # result serialization
    assert "complex_utils::cmult(a[i], b[i])" in k                    # arithmetic via the header
    assert "complex__fixed8_4_array_utils::read_array_slice<16>(aw, a)" in k
    assert "complex__fixed17_9_array_utils::write_array_slice<34>(y, yw)" in k
    # no hand-rolled interleaving / inline formula
    assert "ar * br - ai * bi" not in k and "memcpy" not in k


def test_op_call_dispatch():
    assert "complex_utils::cadd(a[i], b[i])" in _k("cadd")
    assert "complex_utils::csub(a[i], b[i])" in _k("csub")
    assert "complex_utils::conj(a[i])" in _k("conj", binary=False, out_cpp=None or
                                            "std::complex<ap_fixed<9, 5, AP_TRN, AP_WRAP>>",
                                            out_ns="complex__fixed9_5_array_utils",
                                            out_hdr="complex__fixed9_5_array_utils.h", wbo=18)
    assert "y[i] = a[i];" in _k("roundtrip", binary=False, out_cpp=None or
                                "std::complex<ap_fixed<8, 4, AP_TRN, AP_WRAP>>",
                                out_ns="complex__fixed8_4_array_utils",
                                out_hdr="complex__fixed8_4_array_utils.h", wbo=16)


def test_binary_reads_in_b_unary_does_not():
    binary = _k("cadd")
    assert "rw(argv[2])" in binary and "read_array_slice<16>(bw, b)" in binary
    unary = _k("conj", binary=False)
    assert "rw(argv[2])" not in unary and " b[" not in unary


def test_roundtrip_same_type_single_include():
    # round-trip in/out types coincide -> only one array-utils include
    k = _k("roundtrip", binary=False, out_cpp="std::complex<ap_fixed<8, 4, AP_TRN, AP_WRAP>>",
           out_ns="complex__fixed8_4_array_utils", out_hdr="complex__fixed8_4_array_utils.h",
           wbo=16)
    assert k.count("complex__fixed8_4_array_utils.h") == 1


def test_int_kernel_uses_wf_cint():
    k = _k("cmult", in_cpp="wf_cint<8>", out_cpp="wf_cint<17>",
           in_ns="complex__int8_array_utils", out_ns="complex__int17_array_utils",
           in_hdr="complex__int8_array_utils.h", out_hdr="complex__int17_array_utils.h",
           wbi=16, wbo=34)
    assert "static wf_cint<8> a[5]" in k and "static wf_cint<17> y[5]" in k
    assert "complex_utils::cmult(a[i], b[i])" in k
