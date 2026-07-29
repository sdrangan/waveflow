"""fir_block_resource.py — the analytical DSP / BRAM priors for the block FIR.

Phase D1 of ``plans/resource_model.md``: encode the *known physics* and learn only what is left over.
Nothing here is fitted.

The physics is the DSP48E1's geometry — a 25x18 signed multiplier — which decides how many DSPs one
multiply costs:

===============  =======================================================================
``samp_w <= 8``  **0.5** — two narrow multiplies share one DSP.  A packing *win*.
``samp_w <= 18`` **1** — fits directly.
``samp_w <= 25`` **2** — one operand exceeds the 18-bit port, so the product is split.
===============  =======================================================================

and the design's own multiplier count, which is a fact about the two kernels:

* **serial** — one window at a time, so ``NTAP`` multipliers.
* **unrolled** — ``LW`` independent lanes (``fir_compute_unroll_task.h``: *"LW independent windows ->
  LW*NTAP multipliers"*), so ``LW * NTAP``, where ``LW = mem_dwidth // samp_w`` is how many samples a
  memory word carries.

Those two combine into one formula, ``DSP = n_mult * dsp_per_mult(samp_w)``, and the combination
explains something that looks arbitrary in the raw measurements: **the unrolled kernel uses ``2*NTAP``
DSPs at every sample width.**  It is not a coincidence and not a plateau — the lane count *shrinks* as
the width grows while the DSP cost per multiply *rises*, and over this device's step boundaries the two
cancel exactly:

.. code-block:: text

    samp_w   LW = 32//w   DSP/mult   product
       8         4          0.5      2*NTAP
      12         2          1        2*NTAP
      16         2          1        2*NTAP
      24         1          2        2*NTAP

BRAM is the other binding decision, and here the prior is **zero**: every array the compute holds
carries an ``ARRAY_PARTITION`` from its ``add_state`` declaration, which pushes storage out of block
RAM and into LUTs and flip-flops.  Measured across all 24 sweep points, no module reported any BRAM.

The one thing the physics does *not* explain is a constant ``+1`` in the serial-packed case, which is
recorded as a residual rather than rationalized — see :data:`SERIAL_PACK_CORRECTION`.
"""
from __future__ import annotations

import math

from waveflow.calib.resource_model import PriorResourceModel

#: DSP48E1 port geometry: the multiplier is 25x18 signed.
DSP_NARROW_BITS = 8       # at or below this, two multiplies are packed into one DSP
DSP_SINGLE_BITS = 18      # at or below this, one multiply is one DSP
DSP_SPLIT_BITS = 25       # at or below this, a split multiply costs two DSPs

#: Unexplained constant in the serial kernel at ``samp_w <= 8``.  The packed prior predicts
#: ``NTAP/2`` and the measurement is ``NTAP/2 + 1``, at every ``NTAP`` in {8, 16, 32} -- a constant
#: offset, not a scaling error, so it is one multiply that failed to pair rather than a wrong law.
#: Kept explicit and named so it stays visible as *unexplained*: encoding it into the formula would
#: dress up a measurement as physics.
SERIAL_PACK_CORRECTION = 1


def dsp_per_mult(samp_w: int) -> float:
    """DSPs consumed by one ``samp_w x samp_w`` signed multiply on a DSP48E1."""
    w = int(samp_w)
    if w <= DSP_NARROW_BITS:
        return 0.5
    if w <= DSP_SINGLE_BITS:
        return 1.0
    if w <= DSP_SPLIT_BITS:
        return 2.0
    # Beyond the 25-bit port both operands need splitting; the cost grows as the product of the
    # per-operand tile counts.  Untested here -- the sweep stops at 24 -- so it is a documented
    # extrapolation rather than a measured law.
    return math.ceil(w / DSP_SINGLE_BITS) * math.ceil(w / DSP_SPLIT_BITS)


def lane_width(mem_dwidth: int, samp_w: int) -> int:
    """Samples per memory word — the unrolled kernel's lane count.  Floors: a partial lane is no lane."""
    return max(1, int(mem_dwidth) // int(samp_w))


def n_multipliers(ntap: int, samp_w: int, mem_dwidth: int, unroll_lane: bool) -> int:
    """How many multiplies the chosen kernel instantiates."""
    if unroll_lane:
        return int(ntap) * lane_width(mem_dwidth, samp_w)
    return int(ntap)


def dsp_prior(f: dict) -> int:
    """DSP count for a ``FirCompute`` configuration, from geometry alone."""
    ntap, samp_w = int(f["ntap"]), int(f["samp_w"])
    unroll = bool(f.get("unroll_lane"))
    n = n_multipliers(ntap, samp_w, int(f.get("mem_dwidth", 32)), unroll)
    dsp = math.ceil(n * dsp_per_mult(samp_w))
    if not unroll and dsp_per_mult(samp_w) < 1.0:
        dsp += SERIAL_PACK_CORRECTION
    return dsp


def bram_prior(f: dict) -> int:
    """BRAM for a ``FirCompute`` configuration.

    Zero, and not by default: the tap and history arrays carry an ``ARRAY_PARTITION`` from their
    ``add_state`` declaration, which maps storage into LUTs and registers instead of block RAM.  The
    prior asserts that, so a future configuration that *does* spill into BRAM shows up as a prior
    failure rather than passing unnoticed.
    """
    return 0


def fir_compute_prior() -> PriorResourceModel:
    """The zero-parameter DSP + BRAM prior for :class:`~examples.fir_block.fir_block.FirCompute`."""
    return PriorResourceModel(name="fir_compute_prior",
                              formulas={"dsp": dsp_prior, "bram": bram_prior})
