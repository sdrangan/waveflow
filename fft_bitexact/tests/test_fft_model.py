"""S2 gate -- the sequential radix-4 model against the real Vitis FFT.

Bit-exact on every vector whose intermediates stay inside their accumulator ranges.  The
deliberate overflow-boundary vector (v4) is a known, characterised gap; see its xfail.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from fft_bitexact.wf_fft.fft import fft16
from waveflow.utils import fixputils as fp

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "fft_L16_R4_noscale_natural.json"
NAMES = {0: "ramp", 1: "pseudo-random", 2: "impulse", 3: "constant", 4: "alternating-extremes",
         5: "all-most-negative", 6: "all-most-positive", 7: "half-and-half", 8: "prng-a",
         9: "prng-b", 10: "mixed-extremes-a", 11: "mixed-extremes-b"}


@pytest.fixture(scope="module")
def g() -> dict:
    return json.loads(GOLDEN.read_text())


def _sgn(bits, w: int) -> np.ndarray:
    b = np.asarray(bits, dtype=np.int64)
    return np.where(b >= (1 << (w - 1)), b - (1 << w), b)


def _run(g: dict, v: int) -> tuple[int, int]:
    vec = next(x for x in g["vectors"] if x["v"] == v)
    iw, ii, ow = g["in_W"], g["in_I"], g["out_W"]
    xr = _sgn([e["re"] for e in vec["input"]], iw)
    xi = _sgn([e["im"] for e in vec["input"]], iw)
    orr, oii, fo = fft16(xr, xi, iw, ii, g["tw_W"], g["tw_I"])
    assert (fo.W, fo.int_bits) == (ow, g["out_I"]), "output format diverged from the library"
    mr = np.asarray(fp.to_bits(np.asarray(orr, dtype=np.int64), ow))
    mi = np.asarray(fp.to_bits(np.asarray(oii, dtype=np.int64), ow))
    return (int((mr != np.array([e["re"] for e in vec["output"]])).sum()),
            int((mi != np.array([e["im"] for e in vec["output"]])).sum()))


@pytest.mark.parametrize("v", sorted(NAMES), ids=[NAMES[i] for i in sorted(NAMES)])
def test_fft_is_bit_exact(g, v):
    """THE S2 GATE: sequential radix-4 model == Vitis, stored bit for stored bit.

    Twelve vectors, seven of which (v5-v11) were added *after* the overflow rule was derived
    from v4 -- so they are confirmation, not the cases the model was fitted to.  Five of those
    seven sit on or across the accumulator boundary, which is where the model was wrong before.
    """
    bad_re, bad_im = _run(g, v)
    assert (bad_re, bad_im) == (0, 0), f"v{v} diverges: {bad_re} re, {bad_im} im of 16"


def test_every_vector_is_covered(g):
    """The golden must carry all twelve -- a short regen would silently shrink the gate."""
    assert sorted(x["v"] for x in g["vectors"]) == sorted(NAMES)


def test_the_gate_has_teeth(g):
    """A wrong twiddle must fail -- otherwise the gate above proves nothing."""
    import fft_bitexact.wf_fft.fft as m
    real = m.twiddle_stored
    m.twiddle_stored = lambda length, w, i: tuple(np.roll(a, 1) for a in real(length, w, i))
    try:
        bad_re, bad_im = _run(g, 1)
    finally:
        m.twiddle_stored = real
    assert (bad_re + bad_im) > 0, "perturbing the twiddle table did not change the output"


def test_wrapping_at_accumulator_width_would_be_wrong(g):
    """Teeth for the overflow rule specifically.

    Tree additions wrap at their OPERAND width and widen on assignment.  Wrapping at the
    accumulator width instead -- the natural reading of the declared types -- is wrong, and only
    the boundary vectors show it.  If this starts passing, the rule has been "simplified" away.
    """
    import fft_bitexact.wf_fft.fft as m
    real = m.fp._apply_overflow
    seen = {}

    def widened(q, fmt):
        seen["hit"] = True
        return real(q, m._f(fmt.W + 1, fmt.int_bits + 1))

    m.fp._apply_overflow = widened
    try:
        bad = sum(sum(_run(g, v)) for v in (4, 5, 6, 7))
    finally:
        m.fp._apply_overflow = real
    assert seen.get("hit"), "patch never fired -- the test is inert"
    assert bad > 0, "wrapping one bit wider made no difference; the rule is unpinned"
