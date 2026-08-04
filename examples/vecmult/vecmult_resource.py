"""vecmult_resource.py — VecMult's resource model: one structure declaration, four counters.

The whole model is :meth:`VecMultResourceModel.structure`.  Everything else follows from it:

* **DSP, BRAM, URAM** are priced exactly by :mod:`waveflow.calib.device_rules` from the declared
  multipliers and arrays.  Zero fitted parameters, exact at all 16 measured points.
* **LUT and FF** are regressed on basis terms *derived from the same declaration* -- the per-lane
  datapath and the crossbar.  No device rule reaches them, because hard primitives are allocated and
  countable while fabric is what everything else decomposes into.

There is deliberately no second place to state the basis.  Declaring a ``Crossbar`` is what puts
``LW^2`` in the fit, so "a bad held-out error means a missing structure" is actionable: you fix the
declaration, not the polynomial.
"""
from __future__ import annotations




#: The basis LUT and FF are regressed on is **derived** from
#: :meth:`~examples.vecmult.vecmult.VecMult.resource_structure` -- declaring a ``Crossbar`` is what
#: puts ``LW^2`` in the fit.  Recorded here only as the finding that chose the declaration: the
#: obvious alternative (``dwid`` and ``log2(vlen)``) reaches 43% / 52% error because it assumes a
#: per-lane datapath and a counter and misses the crossbar entirely.  Leave-one-out on the derived
#: basis: LUT 0.00%, FF 1.73%.

def vec_mult_samples() -> list:
    """``[(elaborated comp, measured counters), ...]`` for the LUT/FF fit.

    A callable rather than a list so :meth:`~waveflow.calib.vitis_model.VitisResourceModel.load_or_fit`
    only pays to elaborate 15 components when there is no published artifact to load.

    Trained on the **in-BRAM** points only: the LUTRAM corner is a different regime, and asking one
    line to span a discontinuity does not make it better there -- it makes it worse everywhere else.
    """
    from waveflow.build.elaborate import elaborate

    from examples.vecmult.vecmult import VecMult
    from examples.vecmult.vecmult_corpus import in_bram_points

    return [(elaborate(VecMult, {"dwid": d, "vlen": v}, name="fit"), m)
            for v, d, m in in_bram_points()]
