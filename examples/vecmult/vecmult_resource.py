"""vecmult_resource.py — VecMult's model kind, and the regime its fit is trained on.

VecMult declares :meth:`~examples.vecmult.vecmult.VecMult.resource_structure` and picks a kind in
:meth:`~examples.vecmult.vecmult.VecMult.get_rm`, because a module's model belongs on the module.
What lives here is the one *modelling* decision that is neither structure nor device physics: **which
measurements the LUT/FF regression is allowed to see.**

What follows from the structure declaration, once:

* **DSP, BRAM, URAM** are priced exactly by :mod:`waveflow.calib.device_rules` from the declared
  multipliers and arrays -- the :class:`~waveflow.calib.vitis_model.VitisDerived` half of the model.
  Zero fitted parameters, exact at all 16 measured points.
* **LUT and FF** are regressed on basis terms *derived from the same declaration* -- the per-lane
  datapath and the crossbar.  No device rule reaches them, because hard primitives are allocated and
  countable while fabric is what everything else decomposes into.

:class:`~waveflow.calib.vitis_model.VitisResourceModel` composes those two halves as a
:class:`~waveflow.calib.calib.ConcatCalibModel`, so each counter comes from whichever is honest for
it and the weakest-link confidence is the shared machinery's job rather than this example's.

There is deliberately no second place to state the basis.  Declaring a ``Crossbar`` is what puts
``LW^2`` in the fit, so "a bad held-out error means a missing structure" is actionable: you fix the
declaration, not the polynomial.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from waveflow.calib.device_rules import bram_estimate
from waveflow.calib.resource_model import InterfaceResourceModel, boundary_signature
from waveflow.calib.vitis_model import VitisResourceModel

#: The basis LUT and FF are regressed on is **derived** from
#: :meth:`~examples.vecmult.vecmult.VecMult.resource_structure` -- declaring a ``Crossbar`` is what
#: puts ``LW^2`` in the fit.  Recorded here only as the finding that chose the declaration: the
#: obvious alternative (``dwid`` and ``log2(vlen)``) reaches 43% / 52% error because it assumes a
#: per-lane datapath and a counter and misses the crossbar entirely.  Leave-one-out on the derived
#: basis: LUT 0.00%, FF 1.73%.


@dataclass
class VecMultResourceModel(VitisResourceModel):
    """The Vitis split, with the fabric fit restricted to the **block-RAM regime**.

    The grid spans a discontinuity.  At the shallowest banks the buffer leaves block RAM for LUTRAM,
    and its storage reappears *in the fabric the fit is trying to predict* -- so that point's LUT and
    FF are describing different hardware.  Training across it does not make the model better at the
    corner; it makes it worse everywhere else, which is the wrong trade when the corner is one point
    of sixteen.

    The exclusion is a **rule applied to the recorded structure**, not a coordinate.  A row is kept
    when :func:`~waveflow.calib.device_rules.bram_estimate` says that row's declared banks bound to
    block RAM -- the same function the derived half uses to *predict* the counter.  So the regime
    boundary cannot drift away from the threshold that defines it, and a future grid point on the
    LUTRAM side is excluded automatically rather than needing to be noticed.
    """

    #: The top-level shell, as a real :class:`~waveflow.calib.resource_model.InterfaceResourceModel`
    #: keyed on the boundary -- the same class and the same lookup a multi-task composite uses.  It is
    #: a separate term rather than folded into the fabric fit because it *is* a separate thing: the
    #: modules are measured per module, and this is what the design costs beyond them.
    shell: Any = None

    def get_params(self, comp, **runtime) -> dict:
        """The Vitis params, plus the boundary the shell term is keyed on."""
        params = dict(super().get_params(comp, **runtime))
        if self.shell is not None:
            params.update(self.shell.get_params(comp, **runtime))
        return params

    def predict_feat(self, row) -> dict:
        """The modules' counters plus the integration term.

        ``compose`` sums a module's own cost over the hierarchy, and for a single-task design this
        model *is* the whole hierarchy -- so the shell has nowhere else to live.  On a composite it
        would be the parent's own term instead, which is exactly where ``fir_block`` puts it.
        """
        out = dict(super().predict_feat(row))
        if self.shell is not None:
            for c, v in self.shell.predict_feat(row).items():
                if v:
                    out[c] = int(out.get(c, 0)) + int(v)
        return out

    def corpus(self):
        """The store's corpus, filtered to rows whose buffer landed in block RAM."""
        db = super().corpus()
        if db is None or not len(db):
            return db
        need = ("mem0_banks", "mem0_depth", "mem0_elem_bits")
        if any(c not in db.df.columns for c in need):
            return db                       # nothing declared a memory; nothing to filter on
        keep = [bram_estimate(int(r["mem0_banks"]), int(r["mem0_depth"]),
                              int(r["mem0_elem_bits"]), self._part()).binding == "bram"
                for r in db.df.to_dict("records")]
        db.df = db.df[keep].reset_index(drop=True)
        return db


def vec_mult_samples() -> list:
    """``[(elaborated comp, measured counters), ...]`` -- the pre-record-store fit input.

    Kept for the **no-platform** path: with no store to read, the committed grid in
    :mod:`examples.vecmult.vecmult_corpus` is the only corpus there is.  A design whose measurements
    have been filed into a platform library should not use this -- see
    :meth:`~examples.vecmult.vecmult.VecMult.get_rm`, which reads the store.

    Trained on the **in-BRAM** points only, for the reason
    :class:`VecMultResourceModel` documents.
    """
    from waveflow.build.elaborate import elaborate

    from examples.vecmult.vecmult import VecMult
    from examples.vecmult.vecmult_corpus import in_bram_points

    return [(elaborate(VecMult, {"dwid": d, "vlen": v}, name="fit"), m)
            for v, d, m in in_bram_points()]


def vec_mult_shell() -> InterfaceResourceModel:
    """The **integration term** -- ``top - sum(modules)`` -- as a boundary-keyed table.

    Negative here, and that is information rather than an error.  In a design with several tasks this
    term is substantial and positive: it holds the ``m_axi`` adapters, the inter-task FIFOs, the
    AXI-Lite control block and the DATAFLOW shell, and on ``examples/fir_block`` it is 1984 LUT --
    29% of the design.  VecMult is the rare opposite: **one** task and no adapters, so there is
    almost nothing at the boundary for the term to contain, and what remains is Vitis optimizing two
    LUTs away as it flattens the single instance into the top.

    One entry per measured port width, built by elaborating a probe at each and asking it for its
    boundary signature -- so the key is computed the same way the lookup will compute it, and a table
    keyed by hand cannot drift the first time the boundary changes shape.  Measured invariant across
    width as well as configuration, which is itself the finding: the signature changes with ``dwid``
    and the term does not, so this is flattening slack rather than interface logic.
    """
    from waveflow.build.elaborate import elaborate

    from examples.vecmult.vecmult import VecMult
    from examples.vecmult.vecmult_corpus import GRID, INTEGRATION_TERM

    table = {}
    for dwid in sorted({d for _v, d in GRID}):
        probe = elaborate(VecMult, {"dwid": dwid, "vlen": 4096}, name="probe")
        table[boundary_signature(probe)] = dict(INTEGRATION_TERM)
    return InterfaceResourceModel(name="vec_mult_shell", table=table)
