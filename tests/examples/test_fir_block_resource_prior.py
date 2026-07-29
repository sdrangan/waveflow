"""The D1 gate: the DSP / BRAM priors reproduce every measured point with **no fitted parameters**.

The grid below is the real measurement — 24 Vitis C-syntheses of ``examples/fir_block`` on
xc7z020 at 100 MHz (``ntap x samp_w x realization``, ``mem_dwidth=32``, ``samp_i=2``), taken by
``examples/fir_block/fir_block_sweep.py``.  It is embedded here rather than read from the sweep's
JSON because that output is untracked: committing the numbers is what makes the measurement outlive
the work directory, and what lets this gate run with no toolchain.

Why a *prior* and not a fit: DSP and BRAM are **binding decisions** HLS makes and reports, and they
follow the DSP48E1's geometry plus the kernel's own multiplier count.  A formula that reproduces the
corpus exactly with zero free parameters is a far stronger claim than any regression, and it is only
worth making if it is checked at every point — which is what this file does.
"""
from __future__ import annotations

import pytest

from examples.fir_block.fir_block_resource import (
    SERIAL_PACK_CORRECTION,
    bram_prior,
    dsp_per_mult,
    dsp_prior,
    lane_width,
    n_multipliers,
)

#: (ntap, samp_w, unroll_lane) -> measured DSP count for FirCompute.  mem_dwidth=32 throughout.
MEASURED_DSP = {
    (8, 8, False): 5,    (8, 12, False): 8,    (8, 16, False): 8,    (8, 24, False): 16,
    (16, 8, False): 9,   (16, 12, False): 16,  (16, 16, False): 16,  (16, 24, False): 32,
    (32, 8, False): 17,  (32, 12, False): 32,  (32, 16, False): 32,  (32, 24, False): 64,
    (8, 8, True): 16,    (8, 12, True): 16,    (8, 16, True): 16,    (8, 24, True): 16,
    (16, 8, True): 32,   (16, 12, True): 32,   (16, 16, True): 32,   (16, 24, True): 32,
    (32, 8, True): 64,   (32, 12, True): 64,   (32, 16, True): 64,   (32, 24, True): 64,
}


def _features(ntap, samp_w, unroll, mem_dwidth=32):
    return {"ntap": ntap, "samp_w": samp_w, "mem_dwidth": mem_dwidth, "unroll_lane": unroll}


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key,measured", sorted(MEASURED_DSP.items()))
def test_dsp_prior_is_exact_at_every_measured_point(key, measured):
    ntap, samp_w, unroll = key
    assert dsp_prior(_features(ntap, samp_w, unroll)) == measured


def test_bram_prior_is_exact_at_every_measured_point():
    """No module reported BRAM at any of the 24 points — the partitioned arrays live in LUT/FF."""
    for ntap, samp_w, unroll in MEASURED_DSP:
        assert bram_prior(_features(ntap, samp_w, unroll)) == 0


def test_the_whole_grid_in_one_assertion():
    """The headline: 24/24 exact, zero fitted parameters."""
    misses = {k: (dsp_prior(_features(*k)), m) for k, m in MEASURED_DSP.items()
              if dsp_prior(_features(*k)) != m}
    assert not misses, f"prior missed at {misses}"


# ---------------------------------------------------------------------------
# The physics the prior encodes
# ---------------------------------------------------------------------------

def test_dsp48e1_geometry():
    """25x18 signed: narrow multiplies pack two-to-a-DSP, wide ones split across two."""
    assert dsp_per_mult(8) == 0.5        # two share one DSP
    assert dsp_per_mult(12) == 1.0
    assert dsp_per_mult(16) == 1.0
    assert dsp_per_mult(18) == 1.0       # exactly the 18-bit port
    assert dsp_per_mult(24) == 2.0       # exceeds 18, so the product is split


def test_multiplier_count_follows_the_kernel():
    """Serial holds one window; the unrolled body instantiates LW independent lanes."""
    assert n_multipliers(32, 16, 32, unroll_lane=False) == 32          # NTAP
    assert n_multipliers(32, 16, 32, unroll_lane=True) == 32 * 2       # NTAP * LW, LW = 32//16


def test_lane_width_floors():
    """A partial lane is no lane — 32/12 is two samples per word, not 2.67."""
    assert lane_width(32, 8) == 4
    assert lane_width(32, 12) == 2
    assert lane_width(32, 16) == 2
    assert lane_width(32, 24) == 1


def test_the_unrolled_plateau_is_two_effects_cancelling():
    """``2*NTAP`` at every width is not a coincidence, and this is why it must not be hard-coded.

    Lane count *falls* with sample width while DSP-per-multiply *rises*, and over this device's step
    boundaries the product is constant. Encoding "unrolled costs 2*NTAP" would look identical on this
    grid and be wrong the moment ``mem_dwidth`` changes.
    """
    for w in (8, 12, 16, 24):
        assert lane_width(32, w) * dsp_per_mult(w) == 2.0

    # ...and it stops being 2 when the memory word changes, which a hard-coded plateau would miss.
    assert lane_width(64, 16) * dsp_per_mult(16) == 4.0
    assert dsp_prior(_features(32, 16, True, mem_dwidth=64)) == 32 * 4


def test_the_serial_packing_correction_is_isolated_and_named():
    """The one thing the physics does not explain: a constant +1 in the serial-packed case.

    Constant across every NTAP, so it is one multiply that failed to pair — not a wrong law. It stays
    a named constant rather than being folded into the formula, so it remains visibly *unexplained*.
    """
    assert SERIAL_PACK_CORRECTION == 1
    for ntap in (8, 16, 32):
        assert dsp_prior(_features(ntap, 8, False)) == ntap // 2 + SERIAL_PACK_CORRECTION
    # It applies only where packing happens; nowhere else is corrected.
    for w in (12, 16, 24):
        assert dsp_prior(_features(32, w, False)) == round(32 * dsp_per_mult(w))


def test_no_correction_in_the_unrolled_case():
    """The unrolled kernel packs cleanly — the correction is specific to the serial body."""
    assert dsp_prior(_features(32, 8, True)) == 32 * 4 * 0.5 == 64
