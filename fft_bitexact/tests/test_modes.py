"""S4 gate -- all three ``scaling_mode_enum`` values, bit-exact.

Golden: ``golden/fft_L16_R4_modes.json`` from ``cpp/dump_modes.cpp``, which runs the real
``xf::dsp::fft::fft<>`` under each mode and also traces every declared width.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from fft_bitexact.wf_fft.fft import GROW_TO_MAX_WIDTH, NO_SCALING, SCALE, fft16
from waveflow.utils import fixputils as fp

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "fft_L16_R4_modes.json"
MODES = [NO_SCALING, SCALE, GROW_TO_MAX_WIDTH]


@pytest.fixture(scope="module")
def g() -> dict:
    return json.loads(GOLDEN.read_text())


def _sgn(bits, w: int) -> np.ndarray:
    b = np.asarray(bits, dtype=np.int64)
    return np.where(b >= (1 << (w - 1)), b - (1 << w), b)


def _mode(g: dict, name: str) -> dict:
    return next(m for m in g["modes"] if m["mode"] == name)


@pytest.mark.parametrize("mode", MODES)
def test_mode_is_bit_exact(g, mode):
    """THE S4 GATE: every mode, every vector, stored bit for stored bit."""
    m = _mode(g, mode)
    bad = 0
    for vec in m["vectors"]:
        xr = _sgn([e["re"] for e in vec["input"]], g["in_W"])
        xi = _sgn([e["im"] for e in vec["input"]], g["in_W"])
        orr, oii, fo = fft16(xr, xi, g["in_W"], g["in_I"], g["tw_W"], g["tw_I"], mode=mode)
        assert (fo.W, fo.int_bits) == (m["out_W"], m["out_I"]), f"{mode}: output format diverged"
        mr = np.asarray(fp.to_bits(np.asarray(orr, dtype=np.int64), m["out_W"]))
        mi = np.asarray(fp.to_bits(np.asarray(oii, dtype=np.int64), m["out_W"]))
        bad += int((mr != np.array([e["re"] for e in vec["output"]])).sum()
                   + (mi != np.array([e["im"] for e in vec["output"]])).sum())
    assert bad == 0, f"{mode}: {bad} mismatches across {len(m['vectors'])} vectors"


def test_scale_holds_the_width_and_the_others_do_not(g):
    """The modes must actually differ -- otherwise one gate is testing three copies of one thing.

    ``SCALE`` keeps the width fixed and lets integer bits grow: that *is* the per-stage right
    shift.  The other two widen.  This also guards the golden: if a regen ever emitted the same
    mode three times, the parametrised test above would pass and mean nothing.
    """
    outs = {m["mode"]: (m["out_W"], m["out_I"]) for m in g["modes"]}
    assert outs[SCALE] == (g["in_W"], g["in_I"] + 5), "SCALE should not widen"
    assert outs[NO_SCALING][0] > g["in_W"], "NO_SCALING should widen"
    assert outs[GROW_TO_MAX_WIDTH][0] > g["in_W"], "GROW_TO_MAX_WIDTH should widen"
    assert len({m["mode"] for m in g["modes"]}) == 3


def test_modes_share_the_same_inputs(g):
    """dump_modes.cpp restates the 12 vectors from dump_fft.cpp; they must not drift apart."""
    ref = json.loads((GOLDEN.parent / "fft_L16_R4_noscale_natural.json").read_text())
    a = {v["v"]: v["input"] for v in ref["vectors"]}
    for m in g["modes"]:
        for vec in m["vectors"]:
            assert vec["input"] == a[vec["v"]], f"{m['mode']} v{vec['v']} input drifted"


def test_scale_actually_shifts(g):
    """Teeth: modelling SCALE without the fractional drop must fail.

    ``SCALE``'s accumulator is the same width with one more integer bit, so converting into it
    discards a fractional bit.  Skipping that -- wrapping only -- is the mistake this mode
    invites, and it is silent on the width check.
    """
    import fft_bitexact.wf_fft.fft as mod
    real = mod._accumulate
    mod._accumulate = lambda a, b, operand, target: mod.fp._apply_overflow(a + b, operand)
    try:
        m = _mode(g, SCALE)
        vec = m["vectors"][1]
        xr = _sgn([e["re"] for e in vec["input"]], g["in_W"])
        xi = _sgn([e["im"] for e in vec["input"]], g["in_W"])
        orr, _, _ = fft16(xr, xi, g["in_W"], g["in_I"], g["tw_W"], g["tw_I"], mode=SCALE)
        mr = np.asarray(fp.to_bits(np.asarray(orr, dtype=np.int64), m["out_W"]))
        bad = int((mr != np.array([e["re"] for e in vec["output"]])).sum())
    finally:
        mod._accumulate = real
    assert bad > 0, "dropping the fractional convert made no difference; SCALE is unpinned"
