"""The two complex primitives the butterfly is built from, against a C++ golden.

Golden: ``golden/cxops_d16_2_t18_2.json`` from ``cpp/dump_cxops.cpp``, which uses the library's
own ``complexMultiply`` -- not a reimplementation.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from fft_bitexact.wf_fft.cxquant import (
    complex_from_format,
    complex_multiply,
    cquantize,
    product_format,
)
from waveflow.hw.dataschema import DataArray
from waveflow.utils import complexutils as cx
from waveflow.utils import fixputils
from waveflow.utils.fixputils import Format, OMode, QMode

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "cxops_d16_2_t18_2.json"


@pytest.fixture(scope="module")
def g() -> dict:
    return json.loads(GOLDEN.read_text())


def _f(w: int, i: int) -> Format:
    return Format(W=w, int_bits=i, signed=True, q_mode=QMode.AP_TRN, o_mode=OMode.AP_WRAP)


def _sgn(bits, w: int) -> np.ndarray:
    b = np.asarray(bits, dtype=np.int64)
    return np.where(b >= (1 << (w - 1)), b - (1 << w), b)


def test_product_format_matches_the_library(g):
    """``FFTMultiplicationTraits``: max-of-formats, AP_TRN/AP_WRAP -- not sum, not full precision."""
    p = product_format(_f(g["data"]["W"], g["data"]["I"]), _f(g["twiddle"]["W"], g["twiddle"]["I"]))
    assert (p.W, p.int_bits) == (g["product"]["W"], g["product"]["I"])
    assert (p.q_mode, p.o_mode) == (QMode.AP_TRN, OMode.AP_WRAP)


def test_cquantize_is_bit_exact(g):
    """Complex requantize == the ap_fixed cast, on stored bits."""
    S, P = g["src"], g["product"]
    src, dst = _f(S["W"], S["I"]), _f(P["W"], P["I"])
    rows = g["requantize"]
    a = DataArray.specialize(complex_from_format(src), max_shape=(len(rows),))(
        cx.make_complex(_sgn([r["src_re"] for r in rows], S["W"]),
                        _sgn([r["src_im"] for r in rows], S["W"]), src))
    v = np.asarray(cquantize(a, dst).val)
    assert np.asarray(fixputils.to_bits(cx.re_of(v), P["W"])).tolist() == [r["dst_re"] for r in rows]
    assert np.asarray(fixputils.to_bits(cx.im_of(v), P["W"])).tolist() == [r["dst_im"] for r in rows]


def test_complex_multiply_is_bit_exact(g):
    """THE butterfly multiply: partial products truncated into T_op1 before combining."""
    D, T, P = g["data"], g["twiddle"], g["product"]
    fd, ft, fp = _f(D["W"], D["I"]), _f(T["W"], T["I"]), _f(P["W"], P["I"])
    rows = g["complex_multiply"]
    re, im = complex_multiply(_sgn([r["a_re"] for r in rows], D["W"]),
                              _sgn([r["a_im"] for r in rows], D["W"]), fd,
                              _sgn([r["b_re"] for r in rows], T["W"]),
                              _sgn([r["b_im"] for r in rows], T["W"]), ft, fp)
    assert np.asarray(fixputils.to_bits(re, P["W"])).tolist() == [r["p_re"] for r in rows]
    assert np.asarray(fixputils.to_bits(im, P["W"])).tolist() == [r["p_im"] for r in rows]


def test_naive_full_product_would_be_wrong(g):
    """Teeth: the obvious recipe -- complexfield.cmult then one cquantize -- does NOT match.

    Wrong in 18/24 real and 23/24 imaginary parts.  If this ever starts passing, someone has
    changed complex_multiply to the naive form and the FFT is silently wrong.
    """
    D, T, P = g["data"], g["twiddle"], g["product"]
    fd, ft, fp = _f(D["W"], D["I"]), _f(T["W"], T["I"]), _f(P["W"], P["I"])
    rows = g["complex_multiply"]
    are, aim = _sgn([r["a_re"] for r in rows], D["W"]), _sgn([r["a_im"] for r in rows], D["W"])
    bre, bim = _sgn([r["b_re"] for r in rows], T["W"]), _sgn([r["b_im"] for r in rows], T["W"])

    rr, fm = fixputils.mult(are, fd, bre, ft)
    ii, _ = fixputils.mult(aim, fd, bim, ft)
    sre, fsub = fixputils.sub(rr, fm, ii, fm)
    ri, _ = fixputils.mult(are, fd, bim, ft)
    ir, _ = fixputils.mult(aim, fd, bre, ft)
    sim, fadd = fixputils.add(ri, fm, ir, fm)

    bad_re = int((np.asarray(fixputils.to_bits(fixputils.quantize(sre, fsub, fp), P["W"]))
                  != np.array([r["p_re"] for r in rows])).sum())
    bad_im = int((np.asarray(fixputils.to_bits(fixputils.quantize(sim, fadd, fp), P["W"]))
                  != np.array([r["p_im"] for r in rows])).sum())
    assert (bad_re, bad_im) == (18, 23), f"expected the naive model to fail 18/23, got {bad_re}/{bad_im}"
