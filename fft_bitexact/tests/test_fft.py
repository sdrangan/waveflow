"""S2 -- the transform itself.  What is established so far, pinned.

The golden (``golden/fft_L16_R4_noscale_natural.json``) is the real
``xf::dsp::fft::fft<>`` run natively; see ``cpp/dump_fft.cpp``.  The bit-exact Python model is
not written yet, so these tests pin the things it will be built against: the golden's shape,
the growth formulas read out of the library, and the output ordering.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "fft_L16_R4_noscale_natural.json"
LOG2_L = 4  # L = 16


@pytest.fixture(scope="module")
def g() -> dict:
    return json.loads(GOLDEN.read_text())


def _signed(bits, w: int) -> np.ndarray:
    b = np.asarray(bits, dtype=np.int64)
    return np.where(b >= (1 << (w - 1)), b - (1 << w), b)


def _complex(entries, w: int, i: int) -> np.ndarray:
    lsb = 2.0 ** -(w - i)
    return (_signed([e["re"] for e in entries], w)
            + 1j * _signed([e["im"] for e in entries], w)) * lsb


def test_golden_shape_and_config(g):
    """Guard the golden -- a bad regen would make everything else vacuous."""
    assert (g["L"], g["R"]) == (16, 4)
    assert g["scaling_mode"] == "SSR_FFT_NO_SCALING"
    assert g["output_order"] == "SSR_FFT_NATURAL"
    assert g["transform_direction"] == "FORWARD_TRANSFORM"
    assert g["butterfly_rnd_mode"] == "TRN"
    assert len(g["vectors"]) == 5
    for v in g["vectors"]:
        assert len(v["input"]) == 16 and len(v["output"]) == 16


def test_no_scaling_growth_formula(g):
    """``FFTOutputTraits`` (NO_SCALING, forward), ``hls_ssr_fft_output_traits.hpp:147-151``::

        OUTPUT_WL = t_inputSizeBits   + log2(L) + 1
        OUTPUT_IL = t_integerPartBits + log2(L) + 1

    Growth is **bounded by L**, not by the operand widths -- so NO_SCALING is not "full
    precision throughout".  The model must requantize to this, not just let formats grow.
    """
    assert g["out_W"] == g["in_W"] + LOG2_L + 1
    assert g["out_I"] == g["in_I"] + LOG2_L + 1
    assert (g["out_W"], g["out_I"]) == (21, 7)


def test_output_is_natural_order(g):
    """``SSR_FFT_NATURAL`` == numpy's ``fft`` ordering, no permutation.

    Ordering is a permutation, not arithmetic: it cannot cause a 1-LSB error but it produces a
    total mismatch that looks like one.  Pinning it here means a later bit-level failure is
    never explained by "maybe the order is wrong".  The tolerance is quantization noise; the
    bit-exact claim is a separate test (S2, not yet written).
    """
    vec = next(v for v in g["vectors"] if v["v"] == 1)   # pseudo-random: no symmetry to hide behind
    x = _complex(vec["input"], g["in_W"], g["in_I"])
    y = _complex(vec["output"], g["out_W"], g["out_I"])
    ref = np.fft.fft(x)

    rel = np.abs(y - ref).max() / np.abs(ref).max()
    assert rel < 1e-4, f"natural-order mismatch, rel={rel:.3g}"

    order = np.argsort([int(format(i, "04b")[::-1], 2) for i in range(16)])
    assert np.abs(y - ref[order]).max() > 1.0, "bit-reversed should NOT match -- guard is inert"


# The S2 gate itself lives in test_fft_model.py.
