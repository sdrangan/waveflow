"""vecmult_resource.py — VecMult's integration term.

Almost nothing lives here, and that is the point.  ``VecMult`` declares
:meth:`~examples.vecmult.vecmult.VecMult.resource_structure` on the module and names a stock
:class:`~waveflow.calib.vitis_model.VitisResourceModel` in
:meth:`~examples.vecmult.vecmult.VecMult.get_rm`; everything the model needs follows from that
declaration and from the measurements already filed in the example's record library.

There used to be a ``VecMultResourceModel`` subclass here holding two things, and both turned out to
be general rather than VecMult's:

* the **integration term** below, which every design has -- merely large and positive on a multi-task
  composite and ``-2`` LUT of flattening slack on a single task;
* a rule keeping the **LUTRAM-regime row** out of the FF fit, which is now
  :meth:`~waveflow.calib.vitis_model.VitisResourceModel.fit_rows`' default: ``lut`` keeps that row
  because :func:`~waveflow.calib.device_rules.lutram_luts` prices what moved into fabric, and any
  counter without such a rule drops it.  A no-op for a design that does not straddle the boundary.

A worked example should show what a user has to write.  A subclass that only re-stated the defaults
was teaching the opposite.
"""
from __future__ import annotations

from waveflow.calib.resource_model import InterfaceResourceModel

def vec_mult_shell(store=None) -> InterfaceResourceModel:
    """The **integration term** -- ``top - sum(modules)`` -- read from the record store.

    Negative here, and that is information rather than an error.  In a design with several tasks this
    term is substantial and positive: it holds the ``m_axi`` adapters, the inter-task FIFOs, the
    AXI-Lite control block and the DATAFLOW shell, and on ``examples/fir_block`` it is 1984 LUT --
    29% of the design.  VecMult is the rare opposite: **one** task and no adapters, so there is
    almost nothing at the boundary for the term to contain, and what remains is Vitis optimizing two
    LUTs away as it flattens the single instance into the top.

    Built from the store rather than from a constant, which is the point: the number is a measurement
    the synthesis already produced, and a transcription of it into source would have the same
    provenance problem the per-module figures used to have.  One record per synthesis, deduplicated by
    boundary -- so the invariance across compute parameters is *derived* from 16 measurements rather
    than asserted, and a point that broke it would raise instead of contradicting a docstring.
    """
    return InterfaceResourceModel(name="vec_mult_shell", store=store,
                                  cls_name="VecMult").load_table()
