"""fir_block_resource.py — the resource models for the block FIR.

Phases D1 and D2 of ``plans/resource_model.md``, and the split between them is the point: **encode the
known physics, learn only what is left over.**

* **D1 — DSP and BRAM: a prior, nothing fitted.** These are *binding decisions* HLS makes and reports,
  so they follow geometry rather than statistics, and the prior reproduces all 24 measured points
  exactly with zero free parameters.
* **D2 — LUT and FF: fitted.** No closed form reaches partitioned storage, pipeline registers, the
  accumulate tree and the address/mux logic, so those are regressed — but from *physically motivated*
  features (:func:`compute_features`), so the fit extrapolates on structure rather than on coincidence.

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

Those two combine into one formula, ``DSP = n_mult * dsp_per_mult(samp_w, PART)``, and the combination
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

from waveflow.calib.device_rules import dsp_per_mult

#: The part this corpus was measured on.  The DSP geometry it implies now comes from
#: :mod:`waveflow.calib.device_rules` rather than from constants duplicated here -- a DSP48E1 is
#: 25x18 and a DSP48E2 is 27x18, and that is a property of the silicon, not of this filter.
PART = "xc7z020clg484-1"

#: Unexplained constant in the serial kernel at ``samp_w <= 8``.  The packed prior predicts
#: ``NTAP/2`` and the measurement is ``NTAP/2 + 1``, at every ``NTAP`` in {8, 16, 32} -- a constant
#: offset, not a scaling error, so it is one multiply that failed to pair rather than a wrong law.
#: Kept explicit and named so it stays visible as *unexplained*: encoding it into the formula would
#: dress up a measurement as physics.
SERIAL_PACK_CORRECTION = 1


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
    dsp = math.ceil(n * dsp_per_mult(samp_w, PART))
    if not unroll and dsp_per_mult(samp_w, PART) < 1.0:
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


# ---------------------------------------------------------------------------
# D2 — the learned part: LUT and FF
# ---------------------------------------------------------------------------

#: Which of the declared terms each counter is regressed on.  The terms themselves are declared by
#: :meth:`~examples.fir_block.fir_block.FirCompute.resource_structure`; this says which subset each
#: counter uses, because the two counters genuinely want different ones:
#:
#: * **FF** tracks storage -- partitioned arrays become registers -- with a multiplier-count term for
#:   the MAC pipeline.
#: * **LUT** needs the accumulator width as well, and is the harder counter regardless: held out over
#:   the reference grid it runs about 10% mean and 25% worst, against FF's 7% / 19%.  That is reported
#:   rather than tuned away, and it is why validation leads with *decision fidelity* instead of
#:   relative error.
#:
#: Both are **pooled across realizations**.  Forking them (serial vs unrolled) was tried and made LUT
#: *worse* -- 12 points against 4 free parameters overfits -- so the realization forks the module
#: *key* and the lookup, but not the regression, which carries the difference through ``n_mult`` and
#: the lane-extended delay line.
FITTED_BASIS = {"ff": ["store_bits", "n_mult"],
                "lut": ["n_mult", "store_bits", "mac_bits"]}


def fir_compute_model(platform=None, store=None):
    """`FirCompute`'s model, **unfitted** -- a stock :class:`VitisResourceModel`.

    Everything configuration-specific reaches it through
    :meth:`~examples.fir_block.fir_block.FirCompute.resource_structure`, so this function carries only
    what is the same at every point: the part, the class it serves, and the per-counter basis.

    Exposed as a function rather than inlined into ``get_rm`` because a fit wants to be re-run against
    a *subset* of the corpus -- leave-one-out is the honest measure of a 4-parameter fit, and it needs
    to build this object 24 times.

    Call ``.load_or_fit()`` for the shipped model, or ``.fit(samples)`` to train it on chosen points.
    """
    from waveflow.calib.vitis_model import VitisResourceModel

    from examples.fir_block.fir_block import FirCompute

    return VitisResourceModel(name="fir_compute", part=PART, platform=platform, store=store,
                              cls_name="FirCompute", comp_class=FirCompute,
                              fit_basis=dict(FITTED_BASIS))


# ---------------------------------------------------------------------------
# F — where the models are installed
# ---------------------------------------------------------------------------
#
# Nowhere in this file.  ``FirCompute.add_rm_self`` and ``FirBlock.add_rm_self`` are declared on the
# classes themselves in ``fir_block.py``, so ``top.add_rm(platform)`` works on a design as imported —
# nothing has to be called first, and a reader looking for a module's model finds it on the module.
#
# They were briefly installed from here by assigning onto the classes, to keep ``fir_block.py`` free
# of calibration imports.  That bought nothing: both methods import what they need inside the body
# anyway, so the design module gained no module-level dependency either way, and the monkey-patch cost
# the reader a level of indirection for it.
#
# What stays here is the model *content* — the priors, the feature transform and the fitted model's
# shape.  Those are calibration concerns and there is no reason for a design module to carry them.
# Only two modules need any of it: ``FirCmdRx``, ``MemRStream`` and ``MemWStream`` keep the inherited
# default, a lookup against the platform store, because they were measured once and their area is a
# fact to recall rather than a function to fit.  Expect that ratio — the fitting work concentrates in
# the few modules that actually move with the parameters being explored.
