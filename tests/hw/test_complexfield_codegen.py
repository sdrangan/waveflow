"""ComplexField C++ codegen protocol — the generated serialization mirrors the Python one.

``ComplexField`` packs interleaved I/Q (``inner.serialize(re)`` then ``inner.serialize(im)``,
``im`` in the high ``inner_bw`` bits) via ``_serialize_recursive``.  Phase 1 adds the matching
**C++ codegen protocol** (``to_uint_expr`` / ``from_uint_expr`` / ``_gen_read_recursive`` /
``_gen_write_recursive`` + include handling) so the general ``array_utils`` packs complex
directly.  These non-vitis tests prove:

1. the Python ``read_array`` / ``write_array`` round-trip over ``DataArray[ComplexField]`` is
   bit-exact (int / fixed / float inners, word_bw 32 & 64);
2. the Python serialization layout is *re in the low ``inner_bw`` bits, im in the high* — the
   exact layout the generated ``to_uint_expr`` (``re | (im << inner_bw)``) reproduces;
3. ``gen_array_utils`` generates a well-formed header for a complex element (the packed,
   single-per-word, and wide multi-word paths), with the right ``cpp_type`` construction and
   ``<complex>`` / ``wf_cint`` include.

Bit-exactness against real Vitis is the Phase-3 gate; here it is the Python spec + structure.
"""
import tempfile
from pathlib import Path

import numpy as np
import pytest

from waveflow.build.build import BuildConfig, BuildDag
from waveflow.build.streamutils import StreamUtilsStep
from waveflow.hw.arrayutils import gen_array_utils, read_array, write_array
from waveflow.hw.complexfield import ComplexField
from waveflow.hw.dataschema import DataArray, FloatField, IntField
from waveflow.hw.fixpoint import FixedField
from waveflow.utils import complexutils as cx


def _inner(kind: str):
    return {
        "int": IntField.specialize(16, signed=True),
        "fixed": FixedField.specialize(8, 4, True),
        "float": FloatField.specialize(32),
    }[kind]


def _operand(kind: str) -> DataArray:
    inner = _inner(kind)
    cf = ComplexField.specialize(inner)
    n = 5
    if kind == "float":
        rng = np.random.default_rng(7)
        val = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64)
    else:
        fmt = inner.get_format() if kind == "fixed" else cx.int_format(16, True)
        re = np.array([-128, -1, 0, 7, 127], dtype=np.int64) if kind == "fixed" else \
            np.array([-32768, -1, 0, 12345, 32767], dtype=np.int64)
        im = np.roll(re, 2)
        val = cx.make_complex(re, im, fmt)
    return DataArray.specialize(cf, max_shape=(n,))(val)


# --- 1. Python read_array/write_array round-trip is bit-exact ------------------
@pytest.mark.parametrize("kind", ["int", "fixed", "float"])
@pytest.mark.parametrize("word_bw", [32, 64])
def test_read_write_array_roundtrip_bit_exact(kind, word_bw):
    da = _operand(kind)
    cf = type(da).element_type
    words = write_array(da, word_bw=word_bw)
    back = read_array(words, cf, word_bw=word_bw, shape=(np.asarray(da.val).shape[0],))
    a, b = np.asarray(da.val), np.asarray(back.val)
    if kind == "float":
        # identical bits (the values round-trip exactly through the IEEE bit view)
        assert np.array_equal(a.real, b.real) and np.array_equal(a.imag, b.imag)
    else:
        assert np.array_equal(a["re"], b["re"]) and np.array_equal(a["im"], b["im"])


# --- 2. the Python layout is re-low / im-high (what to_uint_expr reproduces) ----
@pytest.mark.parametrize("kind", ["int", "fixed", "float"])
def test_serialize_layout_is_re_low_im_high(kind):
    inner = _inner(kind)
    cf = ComplexField.specialize(inner)
    bw = inner.get_bitwidth()
    # one element, in a single wide word so the bit layout is directly inspectable
    word_bw = 2 * bw
    da = _operand(kind)
    one = DataArray.specialize(cf, max_shape=(1,))(np.asarray(da.val)[:1])
    word = int(np.asarray(write_array(one, word_bw=word_bw)).ravel()[0])
    re_bits, im_bits = word & ((1 << bw) - 1), (word >> bw) & ((1 << bw) - 1)

    # the inner field serialized for the same re / im must equal the low / high halves
    rf, imf = inner(), inner()
    v = np.asarray(da.val)[0]
    if kind == "float":
        rf.val, imf.val = float(v.real), float(v.imag)
    else:
        rf.val, imf.val = int(v["re"]), int(v["im"])
    assert int(np.asarray(rf.serialize(word_bw=bw)).ravel()[0]) == re_bits
    assert int(np.asarray(imf.serialize(word_bw=bw)).ravel()[0]) == im_bits

    # the generated to_uint_expr mirrors this: re unshifted, im shifted by inner_bw
    expr = cf.to_uint_expr("v")
    assert cf._cpp_re("v") in expr and f"<< {bw}" in expr


# --- 3. gen_array_utils produces a well-formed header for a complex element -----
@pytest.mark.parametrize("kind", ["int", "fixed", "float"])
def test_gen_array_utils_generates_complex_header(kind):
    inner = _inner(kind)
    cf = ComplexField.specialize(inner)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cfg = BuildConfig(root_dir=root)
        dag = BuildDag()
        dag.add(StreamUtilsStep(output_dir="include"))
        dag.run(cfg)
        path = gen_array_utils(cf, [32, 64], cfg=cfg, streamutils_dir="include")
        hdr = Path(path).read_text(encoding="utf-8")

    expected_include = "#include \"wf_cint.h\"" if kind == "int" else "#include <complex>"
    assert expected_include in hdr
    # both read/write per-element impl specializations construct the cpp_type from (re, im) halves
    assert f"value_type = {cf.cpp_type}" in hdr
    assert "struct read_array_elem_impl<32>" in hdr and "struct read_array_elem_impl<64>" in hdr
    assert "struct write_array_elem_impl<32>" in hdr and "struct write_array_elem_impl<64>" in hdr
    # the retired bulk read_array/write_array are gone; resident arrays use slice/lane methods
    assert "read_array_slice(" in hdr and "write_array_slice(" in hdr
    assert "read_array<32>" not in hdr and "write_array<32>" not in hdr
    # the wide (multi-word, word_bw=32 for a >32-bit element) path declares re/im temps
    if cf.get_bitwidth() > 32:
        assert "__wf_re" in hdr and "__wf_im" in hdr
