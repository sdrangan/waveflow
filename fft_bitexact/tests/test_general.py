"""S5 -- the general ``L = R^S`` model.

``fft_general`` implements the recursive decimation-in-frequency the library uses, for any
``L = R^S``.  It reproduces ``L=16`` exactly.  ``L=64`` is not yet bit-exact; the gap is
localised and recorded in the xfail below rather than left vague.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from waveflow.utils import fixputils as fp

from fft_bitexact.wf_fft.fft import fft16, fft_general, stage_formats

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "golden"


def _sgn(bits, w: int) -> np.ndarray:
    b = np.asarray(bits, dtype=np.int64)
    return np.where(b >= (1 << (w - 1)), b - (1 << w), b)


def _check(golden_name: str, length: int) -> int:
    g = json.loads((GOLDEN_DIR / golden_name).read_text())
    iw, ii, ow = g["in_W"], g["in_I"], g["out_W"]
    bad = 0
    for vec in g["vectors"]:
        xr = _sgn([e["re"] for e in vec["input"]], iw)
        xi = _sgn([e["im"] for e in vec["input"]], iw)
        r, i, fo = fft_general(xr, xi, length, iw, ii, g["tw_W"], g["tw_I"])
        assert (fo.W, fo.int_bits) == (ow, g["out_I"]), f"output format {fo.W},{fo.int_bits}"
        bad += int((np.asarray(fp.to_bits(r, ow)) != np.array([e["re"] for e in vec["output"]])).sum()
                   + (np.asarray(fp.to_bits(i, ow)) != np.array([e["im"] for e in vec["output"]])).sum())
    return bad


def test_output_format_matches_the_library_at_every_size():
    """``in_W + log2(L) + 1``, derived from the measured per-stage rule.

    Checked against what Vitis actually declares: (21,7) at L=16 and (27,13) at L=1024, the
    latter read off a real synthesis run in ../verifyFFT1024.
    """
    for length, stages, want in ((16, 2, (21, 7)), (64, 3, (23, 9)), (1024, 5, (27, 13))):
        g_last = stage_formats(16, 2, stages)[-1][1]
        assert (g_last.W - 1, g_last.int_bits) == want, f"L={length}"


def test_general_model_reproduces_fft16(g_len=16):
    """The two-stage case of the general model must equal the validated L=16 model exactly."""
    g = json.loads((GOLDEN_DIR / "fft_L16_R4_noscale_natural.json").read_text())
    for vec in g["vectors"]:
        xr = _sgn([e["re"] for e in vec["input"]], g["in_W"])
        xi = _sgn([e["im"] for e in vec["input"]], g["in_W"])
        a = fft_general(xr, xi, 16, g["in_W"], g["in_I"], g["tw_W"], g["tw_I"])
        b = fft16(xr, xi, g["in_W"], g["in_I"], g["tw_W"], g["tw_I"])
        assert np.array_equal(np.asarray(a[0], dtype=np.int64), np.asarray(b[0], dtype=np.int64))
        assert np.array_equal(np.asarray(a[1], dtype=np.int64), np.asarray(b[1], dtype=np.int64))


def test_general_model_is_bit_exact_at_L16():
    """THE gate that holds today: the general model against the real Vitis output at L=16."""
    assert _check("fft_L16_R4_noscale_natural.json", 16) == 0


@pytest.mark.xfail(strict=True, reason=(
    "L=64 is structurally correct but not yet bit-exact. Per-stage tracing shows stages 1 and 2 "
    "feed the hardware's exact values in the hardware's exact order (0/64 differ at both), and "
    "all remaining differences are +-1 LSB on 14 of 64 stage-3 inputs. The cause is identified: "
    "the inter-stage rotation reads its twiddle with readQuaterTwiddleTable "
    "(hls_ssr_fft.hpp:111-113), which reconstructs the value from a QUARTER-wave table using "
    "index symmetry plus explicit saturation at L/4 and 3L/4 -- not the direct table read this "
    "model uses. At L=16 the two agree; at L=64 they part by an LSB. Ruled out: index scaling "
    "(full-L vs sub-length table gives identical results) and whether the narrowing happens "
    "inside the multiply or as a separate cast."))
def test_general_model_is_bit_exact_at_L64():
    assert _check("fft_L64_R4_noscale_natural.json", 64) == 0
