"""Tests for ``ComplexField`` + ``DataArray[ComplexField]`` over float / fixed / int.

Covers: specialization metadata, interleaved-I/Q round-trip serialize/deserialize per
inner, the free-function complex arithmetic + the operator surface (``+``/``-``/``*``)
vs the (independently tested) :mod:`waveflow.utils.complexutils` core, result-format
derivation, and the unsigned/mixed-kind guards.
"""
import numpy as np
import pytest

from waveflow.hw.complexfield import ComplexField, cadd, cmult, conj, csub, csum
from waveflow.hw.dataschema import DataArray, FloatField, IntField
from waveflow.hw.fixpoint import FixedField
from waveflow.utils import complexutils as cx
from waveflow.utils.fixputils import Format


# --- builders -----------------------------------------------------------------
def _fixed(W, I, signed=True):  # noqa: E741 — ap_fixed integer-bit count
    return ComplexField.specialize(FixedField.specialize(W, I, signed))


def _int(W, signed=True):
    return ComplexField.specialize(IntField.specialize(W, signed))


def _float(bw):
    return ComplexField.specialize(FloatField.specialize(bw))


def _da(cf, val):
    arr = np.asarray(val)
    return DataArray.specialize(cf, max_shape=(arr.shape[0],))(arr)


def _da_fixed(W, I, re, im, signed=True):  # noqa: E741 — ap_fixed integer-bit count
    fmt = Format(W, I, signed)
    return _da(ComplexField.specialize(FixedField.specialize(W, I, signed)),
               cx.make_complex(re, im, fmt))


def _da_int(W, re, im, signed=True):
    fmt = cx.int_format(W, signed)
    return _da(_int(W, signed), cx.make_complex(re, im, fmt))


# --- specialization metadata --------------------------------------------------
def test_specialize_metadata():
    cf = _fixed(8, 4)
    assert cf.kind == "fixed" and cf.bitwidth == 16
    assert cf.cpp_type == "std::complex<ap_fixed<8, 4, AP_TRN, AP_WRAP>>"
    assert cf.inner_format() == Format(8, 4, True)
    assert cf.is_complex_field is True

    ci = _int(12)
    assert ci.kind == "int" and ci.bitwidth == 24
    assert ci.cpp_type == "wf_cint<12>" and ci.inner_format() == Format(12, 12, True)

    cflt = _float(32)
    assert cflt.kind == "float" and cflt.bitwidth == 64
    assert cflt.cpp_type == "std::complex<float>"
    assert _float(64).cpp_type == "std::complex<double>"

    assert ComplexField.specialize(FixedField.specialize(8, 4)) is cf   # cached


def test_specialize_rejects_non_scalar_inner():
    with pytest.raises(TypeError):
        ComplexField.specialize(DataArray.specialize(IntField.specialize(8)))


def test_init_value_representation():
    assert _float(32).init_value().dtype == np.complex64
    assert _float(64).init_value().dtype == np.complex128
    iv = _fixed(8, 4).init_value()
    assert iv.dtype == cx.complex_dtype(Format(8, 4, True))
    assert int(iv["re"]) == 0 and int(iv["im"]) == 0


# --- round-trip serialize/deserialize (interleaved I/Q) -----------------------
@pytest.mark.parametrize("word_bw", [32, 64])
def test_roundtrip_fixed(word_bw):
    re = np.array([1, -2, 7, -8, 0, 5], dtype=np.int64)
    im = np.array([-3, 4, -1, 6, 7, -7], dtype=np.int64)
    da = _da_fixed(8, 4, re, im)
    cf = da.element_type
    words = da.serialize(word_bw)
    out = _da(cf, da.val).__class__().deserialize(words, word_bw)
    np.testing.assert_array_equal(out.val["re"], re)
    np.testing.assert_array_equal(out.val["im"], im)


@pytest.mark.parametrize("word_bw", [32, 64])
def test_roundtrip_int(word_bw):
    re = np.array([100, -128, 127, 0], dtype=np.int64)
    im = np.array([-50, 5, -1, 64], dtype=np.int64)
    da = _da_int(8, re, im)
    out = da.__class__().deserialize(da.serialize(word_bw), word_bw)
    np.testing.assert_array_equal(out.val["re"], re)
    np.testing.assert_array_equal(out.val["im"], im)


def test_roundtrip_wide_fixed_crosses_words():
    # inner W=24 -> components 24 bits each; at word_bw=32 the imag spills to word 1.
    re = np.array([(1 << 22) - 1, -(1 << 22), 12345], dtype=np.int64)
    im = np.array([-(1 << 22), (1 << 22) - 1, -54321], dtype=np.int64)
    da = _da_fixed(24, 12, re, im)
    out = da.__class__().deserialize(da.serialize(32), 32)
    np.testing.assert_array_equal(out.val["re"], re)
    np.testing.assert_array_equal(out.val["im"], im)


@pytest.mark.parametrize("bw,word_bw", [(32, 32), (32, 64), (64, 64)])
def test_roundtrip_float(bw, word_bw):
    vals = np.array([1 + 2j, -3.5 + 0.25j, 0 - 1j, 7.5 + 2.5j],
                    dtype=np.complex128 if bw == 64 else np.complex64)
    da = _da(_float(bw), vals)
    out = da.__class__().deserialize(da.serialize(word_bw), word_bw)
    np.testing.assert_array_equal(np.asarray(out.val), vals)


# --- arithmetic vs the complexutils core + format derivation ------------------
def test_cmult_fixed_matches_core_and_format():
    re_a, im_a = np.array([1, -2, 3, 7]), np.array([4, 5, -6, -1])
    re_b, im_b = np.array([2, 1, -1, 3]), np.array([-1, 2, 4, 5])
    a, b = _da_fixed(8, 4, re_a, im_a), _da_fixed(8, 4, re_b, im_b)
    out = cmult(a, b)
    exp, r = cx.cmult(np.asarray(a.val), Format(8, 4, True),
                      np.asarray(b.val), Format(8, 4, True))
    assert out.element_type.inner_format() == r == Format(17, 9, True)
    np.testing.assert_array_equal(out.val["re"], exp["re"])
    np.testing.assert_array_equal(out.val["im"], exp["im"])


def test_cadd_csub_int_match_core():
    re_a, im_a = np.array([10, -20, 30]), np.array([5, -5, 15])
    re_b, im_b = np.array([1, 2, -3]), np.array([-4, 8, 9])
    a, b = _da_int(8, re_a, im_a), _da_int(8, re_b, im_b)
    s, _ = cx.cadd(np.asarray(a.val), cx.int_format(8), np.asarray(b.val), cx.int_format(8))
    d, _ = cx.csub(np.asarray(a.val), cx.int_format(8), np.asarray(b.val), cx.int_format(8))
    np.testing.assert_array_equal(cadd(a, b).val["re"], s["re"])
    np.testing.assert_array_equal(csub(a, b).val["im"], d["im"])
    assert cadd(a, b).element_type.kind == "int"
    assert cadd(a, b).element_type.inner_type.get_bitwidth() == 9   # max+1


def test_csum_real_and_complex_full_precision():
    # real: sum 3 rows of ap_fixed<8,4> -> grows int bits by ceil(log2 3)=2 -> <10,6>
    F = FixedField.specialize(8, 4, True)
    a = DataArray.specialize(F, max_shape=(3, 2))(np.array([[1, 2], [3, 4], [5, 6]], dtype=np.int64))
    r = csum(a, axis=0)
    assert r.element_type.get_format() == Format(10, 6, True)
    np.testing.assert_array_equal(np.asarray(r.val), [9, 12])     # exact, no overflow

    # complex: per-component reduction, re/im share the grown format
    v = cx.make_complex(np.array([[1, 2], [3, 4], [5, 6]]),
                        np.array([[10, 20], [30, 40], [50, 60]]), Format(8, 4, True))
    ca = DataArray.specialize(ComplexField.specialize(F), max_shape=(3, 2))(v)
    cr = csum(ca, axis=0)
    assert cr.element_type.inner_format() == Format(10, 6, True)
    np.testing.assert_array_equal(cr.val["re"], [9, 12])
    np.testing.assert_array_equal(cr.val["im"], [90, 120])


def test_conj_fixed_negates_imag():
    re, im = np.array([1, -2, 3]), np.array([4, -5, 6])
    a = _da_fixed(8, 4, re, im)
    out = conj(a)
    assert out.element_type.inner_format() == Format(9, 5, True)
    np.testing.assert_array_equal(out.val["re"], re)
    np.testing.assert_array_equal(out.val["im"], -im)


def test_float_arithmetic_matches_core():
    av = np.array([1 + 2j, -3 + 0.5j], dtype=np.complex64)
    bv = np.array([0.5 - 1j, 4 + 2j], dtype=np.complex64)
    a, b = _da(_float(32), av), _da(_float(32), bv)
    # cadd is plain numpy; cmult is the naive (hardware-faithful) formula, not numpy `*`
    np.testing.assert_array_equal(np.asarray(cadd(a, b).val), av + bv)
    np.testing.assert_array_equal(np.asarray(cmult(a, b).val), cx.cmult_float(av, bv))
    assert cmult(a, b).element_type.kind == "float"
    assert cmult(a, b).element_type.bitwidth == 64   # complex64, no growth


# --- operator surface (+, -, * dispatch to the complex functions) -------------
def test_operators_dispatch_to_complex_functions():
    a = _da_fixed(8, 4, np.array([1, -2, 3]), np.array([4, 5, -6]))
    b = _da_fixed(8, 4, np.array([2, 1, -1]), np.array([-1, 2, 4]))
    for expr, fn in [(a * b, cmult), (a + b, cadd), (a - b, csub)]:
        ref = fn(a, b)
        np.testing.assert_array_equal(expr.val["re"], ref.val["re"])
        np.testing.assert_array_equal(expr.val["im"], ref.val["im"])


# --- guards -------------------------------------------------------------------
def test_unsigned_cmult_conj_raise():
    a = _da_fixed(8, 4, np.array([1, 2]), np.array([3, 4]), signed=False)
    with pytest.raises(NotImplementedError):
        cmult(a, a)
    with pytest.raises(NotImplementedError):
        conj(a)
    # cadd is fine on unsigned
    assert cadd(a, a).element_type.inner_format().signed is False


def test_mixed_kind_raises():
    fx = _da_fixed(8, 4, np.array([1]), np.array([2]))
    it = _da_int(8, np.array([1]), np.array([2]))
    with pytest.raises(TypeError):
        cadd(fx, it)
    with pytest.raises(TypeError):
        cmult(fx, it)


def test_complex_inherits_the_inner_include_dir() -> None:
    """Its array-utils headers land where the inner's do (rotate follow-up #5)."""
    from waveflow.hw.arrayutils import _array_utils_include_path

    inner = IntField.specialize(bitwidth=18, signed=True, include_dir="include")
    cplx = ComplexField.specialize(inner)
    assert cplx.include_dir == "include"
    assert _array_utils_include_path(cplx).startswith("include/")
    # An explicit override still wins, and a default inner leaves the default.
    assert ComplexField.specialize(inner, include_dir="hdr").include_dir == "hdr"
    plain = ComplexField.specialize(IntField.specialize(bitwidth=19, signed=True))
    assert plain.include_dir == "."
