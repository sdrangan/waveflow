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
from typing import TYPE_CHECKING

from waveflow.calib.resource_model import PriorResourceModel

if TYPE_CHECKING:                      # imported lazily at use to keep this module import-light
    from waveflow.calib.resource_model import FittedResourceModel

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


# ---------------------------------------------------------------------------
# D2 — the learned part: LUT and FF
# ---------------------------------------------------------------------------

def fir_compute_basis(comp) -> dict:
    """The **transform**: raw ``HwParam`` values -> physically-motivated basis terms.

    Raw features are the resolved parameters and are never hand-written; this is the basis map
    on top of them, and it belongs to the *model* rather than the module for the same reason
    :attr:`~waveflow.calib.calib.LinCalibModel.transform` does — it must be identical at fit time
    and at predict time, so it lives in exactly one place.

    Chosen for *meaning* rather than convenience, so the fit extrapolates on structure rather than on
    coincidence between parameters:

    * ``n_mult`` — multipliers instantiated (``NTAP``, or ``NTAP*LW`` unrolled).  Drives the
      accumulate tree and whatever multiply logic the DSPs do not absorb.
    * ``store_bits`` — the tap array plus the delay line, in bits.  These arrays are
      ``ARRAY_PARTITION``-ed, so they land in registers, and across the reference grid storage alone
      correlates 0.985 with FF.  The delay line's length is **realization-dependent**: the serial body
      keeps ``NTAP`` entries, the unrolled one keeps ``NTAP + LW - 1`` (it shifts a whole lane per
      beat — ``SH: for (m = NTAP + LW - 2; m >= LW; --m)``), so this feature is one of the two that
      distinguish the kernels.
    * ``acc_bits`` — the accumulator width the format algebra derives (``2W + ceil(log2 NTAP)``),
      which sets how wide every pipeline register in the MAC has to be.
    * ``mac_bits`` — ``n_mult * acc_bits``, the pipeline's register area to first order.
    """
    p = {k: int(getattr(comp, k)) for k in ("ntap", "samp_w", "mem_dwidth")}
    unroll = bool(getattr(comp, "unroll_lane", False))
    lw = lane_width(p["mem_dwidth"], p["samp_w"])
    n_mult = n_multipliers(p["ntap"], p["samp_w"], p["mem_dwidth"], unroll)
    acc_bits = 2 * p["samp_w"] + math.ceil(math.log2(max(2, p["ntap"])))
    # The unrolled body shifts a whole lane per beat, so its history is LW-1 entries longer.
    delay_entries = p["ntap"] + (lw - 1 if unroll else 0)
    return {
        "ntap": p["ntap"], "samp_w": p["samp_w"], "lw": lw, "n_mult": n_mult,
        "acc_bits": acc_bits,
        "store_bits": p["samp_w"] * (p["ntap"] + delay_entries),
        "mac_bits": n_mult * acc_bits,
    }


#: The fitted bases, chosen by held-out (leave-one-out) error over the 24-point reference grid rather
#: than by in-sample R².  Two findings shaped them:
#:
#: * FF tracks **storage** — partitioned arrays become registers — with a multiplier-count term for the
#:   MAC pipeline.  LOO: ~6% mean, ~17% worst.
#: * LUT is genuinely the harder counter: ~10% mean, ~25% worst.  That is reported rather than tuned
#:   away, and it is why validation leads with *decision fidelity* instead of relative error.
#:
#: Both are **pooled across realizations**.  Forking them (serial vs unrolled) was tried and made LUT
#: *worse* — 12 points against 4 free parameters overfits — so the realization forks the module *key*
#: and the lookup, but not the regression, which carries the difference in ``n_mult`` and ``lw``.
FITTED_BASIS = {"ff": ["store_bits", "n_mult"],
                "lut": ["n_mult", "store_bits", "mac_bits"]}


def fir_compute_fitted(platform=None) -> "FittedResourceModel":
    """`FirCompute`'s complete model: the exact prior for DSP/BRAM plus the fit for LUT/FF.

    The prior rides *inside* the fitted model (``prior=``) rather than being combined by a wrapper —
    one object predicts all four counters, with each counter coming from whichever is honest for it.
    Must be :meth:`~waveflow.calib.resource_model.FittedResourceModel.fit` before it predicts LUT/FF.
    """
    from waveflow.calib.resource_model import FittedResourceModel

    return FittedResourceModel(name="fir_compute", targets=("lut", "ff"),
                               basis=dict(FITTED_BASIS), transform=fir_compute_basis,
                               prior=fir_compute_prior(), platform=platform)


# ---------------------------------------------------------------------------
# F — installing the models: the whole author-facing surface for this design
# ---------------------------------------------------------------------------

def install_resource_models() -> None:
    """Give ``FirCompute`` and ``FirBlock`` their ``add_rm_self`` overrides.

    Only two modules need one.  ``FirCmdRx``, ``MemRStream`` and ``MemWStream`` keep the inherited
    default — a lookup against the platform store — because they were measured once and their area is
    a fact to recall rather than a function to fit.  That is the ratio to expect: the fitting work
    concentrates in the few modules that actually move with the parameters being explored.

    Attached here rather than declared in ``fir_block.py`` so the design module stays free of
    calibration imports; a design that wanted them inline would simply define ``add_rm_self`` on the
    class.
    """
    from examples.fir_block.fir_block import FirBlock, FirCompute

    def compute_add_rm_self(self, platform):
        """Prior for the binding decisions, fit for the estimated counters — one model, four counters.

        Loads its fitted coefficients from the committed corpus.  In a design whose corpus lived in the
        platform library this would load from there instead; the corpus is local here because it is
        also the example's test fixture.
        """
        from examples.fir_block.fir_block_corpus import points
        from waveflow.build.elaborate import elaborate

        model = fir_compute_fitted(platform=platform)
        samples = [(elaborate(FirCompute, {"mem_dwidth": 32, "ntap": n, "samp_w": w,
                                           "samp_i": 2, "unroll_lane": u}, name="fit"), m)
                   for n, w, u, m in points()]
        self._resource_model = model.fit(samples)

    def block_add_rm_self(self, platform):
        """The composite's OWN cost: adapters, channel FIFOs, control block, DATAFLOW shell.

        Keyed on boundary structure, because that is what it depends on — measured invariant across 24
        compute configurations and moving only when the memory word width did.
        """
        from waveflow.calib.resource_model import InterfaceResourceModel, boundary_signature
        from examples.fir_block.fir_block_corpus import INTERFACE_BY_MEM_DWIDTH
        from waveflow.build.elaborate import elaborate

        table = {}
        for dw, counters in INTERFACE_BY_MEM_DWIDTH.items():
            probe = elaborate(FirBlock, {"mem_dwidth": dw, "ntap": 32, "samp_w": 16,
                                         "samp_i": 2, "unroll_lane": False}, name="probe")
            table[boundary_signature(probe)] = dict(counters)
        self._resource_model = InterfaceResourceModel(
            name="fir_block_interface", table=table, platform=platform)

    FirCompute.add_rm_self = compute_add_rm_self
    FirBlock.add_rm_self = block_add_rm_self
