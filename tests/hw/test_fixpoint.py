"""Phase 2: integer-backed FixedField + DataArray[FixedField] vector arithmetic.

Covers specialization/format/cpp_type, the stored-int .val + from_real/to_real
round-trip, the scalar .real view, serialize round-trip, and the free arithmetic
functions on DataArray operands — result-format derivation + exact real results
(full-precision intermediates) + quantize vs the fixputils oracle.
"""
from fractions import Fraction

import numpy as np
import pytest

from waveflow.hw.fixpoint import (
    FixedField, add, div, fixed_sum, from_real, mult, quantize, shift, sub, to_real,
)
from waveflow.utils import fixputils
from waveflow.utils.fixputils import Format, OMode, QMode

TRN, RND, WRAP, SAT = QMode.AP_TRN, QMode.AP_RND, OMode.AP_WRAP, OMode.AP_SAT


def test_specialize_format_cpp_type_cache_guard():
    F = FixedField.specialize(8, 4)
    assert F.get_format() == Format(8, 4, True)
    assert F.cpp_type == "ap_fixed<8, 4, AP_TRN, AP_WRAP>"
    assert FixedField.specialize(8, 4, signed=False).cpp_type == "ap_ufixed<8, 4, AP_TRN, AP_WRAP>"
    assert FixedField.specialize(16, 8, q_mode=RND, o_mode=SAT).cpp_type == "ap_fixed<16, 8, AP_RND, AP_SAT>"
    assert FixedField.specialize(8, 4) is FixedField.specialize(8, 4)
    with pytest.raises(NotImplementedError):
        FixedField.specialize(65, 32)               # width guard


def test_val_is_stored_int_dtype():
    da = from_real([1.5, -2.0, 0.0625], FixedField.specialize(8, 4))
    assert np.asarray(da).dtype == np.int64
    np.testing.assert_array_equal(np.asarray(da), [24, -32, 1])
    u = from_real([1.5, 0.0625], FixedField.specialize(8, 4, signed=False))
    assert np.asarray(u).dtype == np.uint64


def test_from_real_to_real_round_trip_on_representable():
    F = FixedField.specialize(8, 4)
    reals = np.array([c / 16 for c in range(-128, 128)], dtype=np.float64)
    np.testing.assert_array_equal(to_real(from_real(reals, F)), reals)


def test_from_real_quantizes_per_mode():
    lsb = 1 / 16
    assert to_real(from_real([1.53], FixedField.specialize(8, 4, q_mode=TRN)))[0] == 1.5
    assert to_real(from_real([0.5 * lsb], FixedField.specialize(8, 4, q_mode=TRN)))[0] == 0.0
    assert to_real(from_real([0.5 * lsb], FixedField.specialize(8, 4, q_mode=RND)))[0] == lsb


def test_scalar_real_view_and_reject_real_assignment():
    F = FixedField.specialize(8, 4)
    assert F(24).real == 1.5                        # stored 24 -> real 1.5
    with pytest.raises(ValueError):
        F(1.5)                                       # reals must go through from_real


def test_serialize_deserialize_round_trip():
    F = FixedField.specialize(12, 6, q_mode=RND, o_mode=SAT)
    da = from_real([1.5, -2.5, 0.5, 7.0], F)
    DA = type(da)
    restored = DA().deserialize(da.serialize(word_bw=32), word_bw=32)
    np.testing.assert_array_equal(np.asarray(restored), np.asarray(da))
    np.testing.assert_array_equal(to_real(restored), to_real(da))


# --- arithmetic: format derivation matches fixputils; results are exact -------
def test_arithmetic_format_derivation_matches_fixputils():
    A, B = FixedField.specialize(8, 4), FixedField.specialize(8, 2)
    a, b = from_real([1.0], A), from_real([1.0], B)
    assert mult(a, b).element_type.get_format() == fixputils.mult_format(A.get_format(), B.get_format())
    assert add(a, b).element_type.get_format() == fixputils.add_format(A.get_format(), B.get_format())
    assert sub(a, b).element_type.get_format() == fixputils.sub_format(A.get_format(), B.get_format())
    assert shift(a, 3).element_type.get_format() == fixputils.shift_format(A.get_format(), 3)


@pytest.mark.parametrize("op,real_op", [
    (mult, lambda x, y: x * y),
    (add, lambda x, y: x + y),
    (sub, lambda x, y: x - y),
])
def test_arithmetic_is_full_precision_exact(op, real_op):
    A, B = FixedField.specialize(8, 4), FixedField.specialize(8, 2)
    a = from_real([1.5, -2.0, 0.5, 7.9375, -8.0], A)
    b = from_real([1.75, 1.5, -1.0, 0.5, -2.0], B)   # all representable in s8_2
    result = op(a, b)
    # compare against the actual stored operand reals -> exact (no intermediate loss)
    np.testing.assert_array_equal(to_real(result), real_op(to_real(a), to_real(b)))


def test_shift_is_lossless_rescale():
    A = FixedField.specialize(8, 4)
    a = from_real([1.5, -2.0, 0.0625], A)
    out = shift(a, 2)
    np.testing.assert_array_equal(np.asarray(out), np.asarray(a))     # bits unchanged
    np.testing.assert_array_equal(to_real(out), to_real(a) * 4)


@pytest.mark.parametrize("q", [TRN, RND])
@pytest.mark.parametrize("o", [WRAP, SAT])
def test_quantize_matches_fixputils_oracle(q, o):
    A = FixedField.specialize(8, 4)
    prod = mult(from_real([3.3, -3.9, 7.0, -8.0], A), from_real([2.5, 1.1, 1.0, 1.0], A))
    target = FixedField.specialize(8, 4, q_mode=q, o_mode=o)
    got = quantize(prod, target)
    exp = fixputils.quantize(np.asarray(prod), prod.element_type.get_format(), target.get_format())
    np.testing.assert_array_equal(np.asarray(got), np.asarray(exp))


def test_sum_of_products_exact_real():
    A = FixedField.specialize(8, 4)
    ra = np.array([1.5, -2.0, 0.5, 1.0, -0.5, 2.0], dtype=np.float64)
    rb = np.array([2.0, 1.5, -1.0, 0.5, 0.25, -1.0], dtype=np.float64)
    a, b = from_real(ra, A), from_real(rb, A)
    acc = fixed_sum(mult(a, b))
    assert acc.element_type.get_format() == fixputils.sum_format(
        fixputils.mult_format(A.get_format(), A.get_format()), len(ra))
    np.testing.assert_array_equal(to_real(acc), [np.dot(to_real(a), to_real(b))])  # bit-exact dot


def test_mixed_sign_arithmetic_raises():
    a = from_real([1.0], FixedField.specialize(8, 4, signed=True))
    b = from_real([1.0], FixedField.specialize(8, 4, signed=False))
    with pytest.raises(NotImplementedError):
        mult(a, b)


def test_derived_over_64_raises():
    A = FixedField.specialize(40, 20)
    a = from_real([1.0], A)
    with pytest.raises(NotImplementedError):
        mult(a, a)                                  # -> W=80


def test_import_location():
    import waveflow.hw.dataschema as ds
    assert not hasattr(ds, "FixedField")            # imported from waveflow.hw.fixpoint


# --- Phase 3: codegen (non-Vitis: assert the generated C++ text) --------------
def test_codegen_array_uses_ap_fixed_bit_helpers():
    import tempfile
    from pathlib import Path

    from waveflow.build.build import BuildConfig
    from waveflow.hw.arrayutils import gen_array_utils
    Q = FixedField.specialize(8, 4, include_dir="include")
    with tempfile.TemporaryDirectory() as td:
        hdr = gen_array_utils(Q, [32], cfg=BuildConfig(root_dir=Path(td)), streamutils_dir="include")
        txt = hdr.read_text(encoding="utf-8")
    assert "using value_type = ap_fixed<8, 4, AP_TRN, AP_WRAP>;" in txt
    assert "streamutils::fixed_to_bits<ap_fixed<8, 4, AP_TRN, AP_WRAP>>" in txt
    assert "streamutils::bits_to_fixed<ap_fixed<8, 4, AP_TRN, AP_WRAP>>" in txt


def test_arith_kernel_renderers():
    from examples.schemas.fixedpoint.kernels import render_binop, render_dot, render_requant
    binop = render_binop("*", "ap_fixed<8, 4, AP_TRN, AP_WRAP>", 8,
                         "ap_fixed<8, 4, AP_TRN, AP_WRAP>", 8,
                         "ap_fixed<16, 8, AP_RND, AP_SAT>", 16)
    assert "y = a * b;" in binop and "y.range(16 - 1, 0)" in binop
    assert "ap_fixed<16, 8, AP_RND, AP_SAT> y" in binop
    requant = render_requant("ap_fixed<16, 8, AP_TRN, AP_WRAP>", 16, "ap_fixed<8, 4, AP_RND, AP_WRAP>", 8)
    assert "y = x;" in requant
    dot = render_dot("ap_fixed<8, 4, AP_TRN, AP_WRAP>", 8, "ap_fixed<8, 4, AP_TRN, AP_WRAP>", 8,
                     "ap_fixed<22, 14, AP_TRN, AP_WRAP>", "ap_fixed<8, 4, AP_TRN, AP_WRAP>", 8)
    assert "acc += a * b;" in dot and "ap_fixed<22, 14, AP_TRN, AP_WRAP> acc = 0;" in dot


# --- division (plans/mimo_cg/mimo_cg_paper_sims.md, step 2.2) ------------------------------


def _trunc_div(n: int, d: int) -> int:
    """Integer division truncating toward zero, in exact Python integers."""
    q = abs(n) // abs(d)
    return q if (n >= 0) == (d > 0) else -q


def test_div_format_is_the_vitis_rule():
    # <Sb + Wa + max(Fb, 0), Sb + Ia + Fb>: the quotient keeps the dividend's fraction bits.
    assert fixputils.div_format(Format(8, 4), Format(8, 4)) == Format(13, 9, True)
    assert fixputils.div_format(Format(33, 9), Format(34, 10)) == Format(58, 34, True)
    assert fixputils.div_format(Format(8, 4, False), Format(8, 4, False)) == Format(12, 8, False)
    assert fixputils.div_format(Format(16, 6), Format(12, 3)).frac_bits == 10
    with pytest.raises(NotImplementedError):
        fixputils.div_format(Format(8, 4), Format(8, 4, False))  # mixed sign
    with pytest.raises(NotImplementedError):
        fixputils.div_format(Format(40, 9), Format(34, 10))  # 1 + 40 + 24 > 64


def test_div_truncates_toward_zero_and_guards_zero():
    S = FixedField.specialize(8, 4)
    a = from_real([1.0, -1.0, 7.0, -8.0, 1.0, 0.5, -0.0625], S)
    b = from_real([3.0, 3.0, 0.0625, -0.0625, 0.0, -2.0, 0.0], S)
    q = div(a, b)
    assert q.element_type.get_format() == Format(13, 9, True)
    # 1/3 = 0.333 -> 0.3125 and -1/3 -> -0.3125 (toward zero, not floor's -0.375); x/0 = 0.
    # -8 / -0.0625 is min / -1 LSB, which Vitis C-sim wraps to -128 (see the test below).
    np.testing.assert_array_equal(to_real(q), [0.3125, -0.3125, 112.0, -128.0, 0.0, -0.25, 0.0])


@pytest.mark.parametrize(
    "fa,fb",
    [((8, 4), (8, 4)), ((16, 6), (12, 3)), ((33, 9), (34, 10)), ((33, 9), (33, 9)), ((20, 9), (16, 10))],
)
def test_div_matches_exact_integer_arithmetic(fa, fb):
    """Stored quotient == trunc((a << Fb) / b) in exact Python integers, and |q - a/b| < 1 LSB."""
    A, B = Format(*fa), Format(*fb)
    rng = np.random.default_rng(17)
    sa = rng.integers(-(1 << (A.W - 1)), 1 << (A.W - 1), size=400)
    sb = rng.integers(-(1 << (B.W - 1)), 1 << (B.W - 1), size=400)
    sb[:8] = [0, 1, -1, 2, -3, 7, (1 << (B.W - 1)) - 1, -(1 << (B.W - 1))]
    q, r = fixputils.div(sa, A, sb, B)
    for x, y, got in zip(sa.tolist(), sb.tolist(), q.tolist(), strict=True):
        if y == 0:
            assert got == 0
            continue
        if x == -(1 << (A.W - 1)) and y == -1:
            continue  # the Vitis C-sim wrap, tested on its own below
        assert got == _trunc_div(x << B.frac_bits, y)
        exact = Fraction(x, 1 << A.frac_bits) / Fraction(y, 1 << B.frac_bits)
        assert abs(Fraction(got, 1 << r.frac_bits) - exact) < Fraction(1, 1 << r.frac_bits)


def test_div_min_by_minus_one_lsb_wraps_like_vitis_csim():
    """Vitis C-sim divides at the shifted dividend's width (Wa + Fb = 12 bits here), so the
    one overflowing quotient, min / -1 LSB = -8 / -0.0625 = +128, wraps to -128.  Measured on
    Vitis 2024.1 (conformance case div_s8_4, pair 220); other negative divisors are exact."""
    A = Format(8, 4)
    q, r = fixputils.div(np.array([-128, -128]), A, np.array([-1, -16]), A)
    np.testing.assert_array_equal(fixputils.to_float(q, r), [-128.0, 8.0])


def test_div_then_quantize_saturates_or_wraps():
    A, B = FixedField.specialize(20, 9), FixedField.specialize(16, 10)
    a = from_real([200.0], A)
    b = from_real([2.0 ** -6], B)  # one LSB of s16_10 (6 fraction bits): a huge quotient
    wide = div(a, b)
    sat = quantize(wide, FixedField.specialize(12, 5, o_mode=SAT))
    wrap = quantize(wide, FixedField.specialize(12, 5, o_mode=WRAP))
    assert to_real(sat)[0] == pytest.approx(16 - 2.0 ** -7)
    assert to_real(wrap)[0] != to_real(sat)[0]


def test_truncate_sign_extends_at_63_bits():
    """``truncate`` once overflowed int64 at wid == 63 (``y - (1 << 63)``); a 63-bit wrap is
    reachable from legal <= 64-bit formats, e.g. a widened ``div`` (plans/mimo_cg step 2.3)."""
    rng = np.random.default_rng(3)
    for wid in (8, 32, 62, 63):
        x = rng.integers(np.iinfo(np.int64).min, np.iinfo(np.int64).max, size=500, dtype=np.int64)
        x[:4] = [0, -1, (1 << (wid - 1)) - 1, -(1 << (wid - 1))]
        ref = [((int(v) + (1 << (wid - 1))) % (1 << wid)) - (1 << (wid - 1)) for v in x]
        assert fixputils.truncate(x, wid, True).tolist() == ref, wid


def test_div_by_the_most_negative_64_bit_divisor():
    """|num| < 2**63 over -2**63 truncates to 0; an abs()-based quotient once returned +/-1."""
    A, B = Format(8, 4), Format(64, 10)  # F2 = 54
    q, _ = fixputils.div(np.array([5, -5, 0]), A, np.array([-(1 << 63)] * 3), B)
    assert q.tolist() == [0, 0, 0]
