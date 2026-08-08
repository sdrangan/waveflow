"""vecmult_corpus.py — the measured resource corpus for VecMult.

The **committed record** of 16 Vitis C-syntheses of ``examples/vecmult`` on xc7z020 at 100 MHz
(``vlen x dwid``, ``samp_w=16``), taken in ~7.6 minutes by ``vecmult_sweep.py``.

It lives here as source rather than as the sweep's JSON because ``results/*.json`` is untracked:
committing the numbers is what makes the measurement outlive the work directory, and what lets the
model gates run with no toolchain installed.  Re-running the sweep regenerates the same grid; this
file is the snapshot the tests and ``docs/examples/vecmult/resmodfit.md`` are written against.

The grid is chosen to separate the two BRAM regimes rather than to cover a box uniformly, because
the point of the example is that the two regimes obey *different-looking* laws that are in fact one
ceiling:

* ``vlen = 512, 1024`` — every bank is shallower than one BRAM18, so the buffer is
  **partition-bound**: BRAM tracks ``LW`` and the data size is irrelevant.
* ``vlen = 16384`` — every bank is deeper than one block, so the buffer is **data-bound**: BRAM is
  16 at every ``LW``, and partitioning is free.
* ``vlen = 4096`` — straddles the knee.

and one corner where the law does not apply at all, which is recorded rather than smoothed away:

* ``(vlen=512, dwid=256)`` — banks 32 deep.  HLS abandoned block RAM for LUTRAM, so BRAM is **0**
  and the storage reappears in fabric (see :data:`LUTRAM_CORNER`).
"""
from __future__ import annotations

import math
from pathlib import Path

from waveflow.calib.device_rules import bram_estimate, dsp_count

#: Sample width the whole grid was measured at.  Fixed for the design, not a swept knob.
SAMP_W = 16

#: The part every point below was measured on.  The device rules are keyed on it, because the block
#: shapes and multiplier ports they encode are properties of the silicon and of nothing else.
PART = "xc7z020clg484-1"

#: ``(vlen, dwid)`` -> measured **whole-design** counters, committed as source.
#:
#: NOT the corpus the model is fitted from.  Since the sweep began filing records, the fit reads the
#: per-module measurements in the platform record store and adds the **integration record**
#: back -- these are ``top``, which is ``module + integration``, and both halves are now measured
#: rather than transcribed.  Two narrower jobs remain:
#:
#: * the **oracle** ``tests/examples/test_vecmult.py`` checks predictions against, which is why it is
#:   committed as source: it runs with no toolchain installed, so a machine without Vitis still
#:   catches a model that stopped reproducing its own measurements;
#: * the fallback corpus for ``add_rm(None)``, where there is no platform and so no store to read.
#:
#: A second independent copy is a feature exactly while one checks the other.  It stops being one the
#: moment both claim to be the source -- so if these ever disagree with the store, the store is right
#: and this is stale.
#: The example's own committed record library -- 16 syntheses filed by module key, promoted out of
#: the sweep's untracked work tier.  Named here so :meth:`~examples.vecmult.vecmult.VecMult.get_rm`
#: can fall back to it when no platform is supplied, which is what lets the model fit itself with no
#: hand-written sample list and no toolchain installed.
COMMITTED_CALIB = Path(__file__).resolve().parent / "calib" / "platforms" / "zynq7020_vecmult"

GRID: dict = {
    (  512,  32): dict(lut= 964, ff= 415, dsp= 2, bram= 2),
    (  512,  64): dict(lut=1370, ff= 593, dsp= 4, bram= 4),
    (  512, 128): dict(lut=2622, ff=1390, dsp= 8, bram= 8),
    (  512, 256): dict(lut=7084, ff=3755, dsp=16, bram= 0),   # LUTRAM corner
    ( 1024,  32): dict(lut= 964, ff= 417, dsp= 2, bram= 2),
    ( 1024,  64): dict(lut=1370, ff= 595, dsp= 4, bram= 4),
    ( 1024, 128): dict(lut=2622, ff=1393, dsp= 8, bram= 8),
    ( 1024, 256): dict(lut=6956, ff=3618, dsp=16, bram=16),
    ( 4096,  32): dict(lut= 964, ff= 421, dsp= 2, bram= 4),
    ( 4096,  64): dict(lut=1370, ff= 599, dsp= 4, bram= 4),
    ( 4096, 128): dict(lut=2622, ff=1399, dsp= 8, bram= 8),
    ( 4096, 256): dict(lut=6956, ff=3622, dsp=16, bram=16),
    (16384,  32): dict(lut= 964, ff= 425, dsp= 2, bram=16),
    (16384,  64): dict(lut=1370, ff= 603, dsp= 4, bram=16),
    (16384, 128): dict(lut=2622, ff=1405, dsp= 8, bram=16),
    (16384, 256): dict(lut=6956, ff=3626, dsp=16, bram=16),
}

#: The one point where the BRAM prior does not apply, kept explicit.  At a bank depth of 32 HLS
#: declined block RAM entirely; the storage went to fabric, costing +137 FF and +128 LUT against the
#: same lane count with the buffer in block RAM.  Named so it stays visible as a *regime*, not as
#: prior error -- folding it into a fitted coefficient would hide a discontinuity as a slope.
LUTRAM_CORNER = (512, 256)

#: The basis LUT and FF are fitted on, and the finding that chose it.
#:
#: The obvious features -- ``dwid`` and ``log2(vlen)`` -- reach only **43% / 52%** error, because the
#: cost is not linear in the lane count at all.  ``LW + LW^2 + LW^2*log2(LW)`` is the signature of a
#: **crossbar**: the runtime length ``n`` means the final beat carries a variable number of lanes at
#: variable positions, so every lane needs a comparator against ``nlane`` and the pack/unpack becomes
#: an LW-way variable-position mux -- ~LW^2 switches with log2(LW) select depth.
#:
#: Leave-one-out over the 15 in-BRAM points: **LUT 0.00%**, FF 0.66% mean / 1.73% max.
#:
#: The lesson is that the right basis came from asking what the hardware *is*, not from adding
#: polynomial terms until the residual fell.  Note also that ``vlen`` does not appear in LUT at all:
#: it is byte-identical across all four lengths at each width.
FITTED_BASIS = {"lut": ["lw", "lw2", "lw2_log2lw"],
                "ff":  ["lw", "lw2", "lw2_log2lw"]}



def fit_basis(vlen: int, dwid: int) -> dict:
    """Raw parameters -> the basis terms LUT/FF are regressed on.

    Owned by the corpus rather than by the caller so the raw -> basis map is identical at fit time
    and at predict time -- the same reason a model owns its transform.
    """
    lw = lane_width(dwid)
    return {"lw": float(lw), "lw2": float(lw * lw),
            "lw2_log2lw": float(lw * lw * math.log2(lw)) if lw > 1 else 0.0}


def lane_width(dwid: int, samp_w: int = SAMP_W) -> int:
    """Samples per stream word -- the lane count, and the buffer's partition factor."""
    return max(1, int(dwid) // int(samp_w))


def dsp_prior(vlen: int, dwid: int) -> int:
    """DSPs: one multiply per lane.

    The whole model is the **structural count** -- ``n_mult = LW``, operands ``SAMP_W`` wide.  How
    much a device charges for such a multiply is
    :func:`~waveflow.calib.device_rules.dsp_count`'s business, not this design's.

    Exact at all 16 points, with zero fitted parameters.
    """
    return dsp_count(lane_width(dwid), SAMP_W, PART)


def bram_prior(vlen: int, dwid: int) -> int:
    """BRAM: ``LW`` banks (the ``ARRAY_PARTITION`` factor), each ``vlen/LW`` deep.

    Again only the **structure** is stated here.  The block shape, the rounding, and the band where
    HLS prefers distributed RAM all live in
    :func:`~waveflow.calib.device_rules.bram_estimate`.

    Exact at all 16 points -- including the corner where the answer is 0 because the buffer did not
    land in block RAM at all.
    """
    lw = lane_width(dwid)
    return bram_estimate(lw, int(vlen) // lw, SAMP_W, PART).blocks


def points() -> list:
    """``[(vlen, dwid, measured), ...]`` over the whole grid, in a stable order."""
    return [(v, d, dict(m)) for (v, d), m in sorted(GRID.items())]


def in_bram_points() -> list:
    """The grid minus the LUTRAM corner -- the points a LUT/FF fit should be trained on.

    Training on all 16 would ask one line to span a discontinuity, which does not make the model
    better at the corner; it makes it worse everywhere else.
    """
    return [(v, d, m) for v, d, m in points() if (v, d) != LUTRAM_CORNER]
