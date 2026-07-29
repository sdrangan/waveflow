"""resource_model.py — predicting a design's area by composing per-module models.

The composition rule is one line, applied recursively:

.. code-block:: text

    predict(comp) = comp's OWN model  +  Σ predict(child)

A leaf's own cost is its whole cost.  A **composite's** own cost is what it adds *beyond* its children
— the ``m_axi`` adapters, the inter-task FIFOs, the AXI-Lite control block, the DATAFLOW shell.  That
is not a special third term bolted on: it is the same rule at the next level up, and it is exactly what
a synthesis report measures as ``top row − Σ task rows``.  Definition and measurement coincide with
nothing left over.

**Most modules need a table, not a model.**  Measured across a 24-point ``ntap × samp_w × realization``
sweep of ``examples/fir_block``, the two memory modules resolved to **one** configuration each and the
command receiver to four — they do not vary with the knobs being explored, so there is nothing to fit.
Only the compute module moved with every knob.  So the kinds here are deliberately lopsided:

* :class:`LookupResourceModel` — return the stored measurement for this module key.  No free
  parameters, and it cannot interpolate, which it says honestly rather than guessing.
* :class:`PriorResourceModel` — a formula.  DSP and BRAM are *binding decisions* HLS reports, and they
  follow known physics (a multiply too wide for the DSP takes two; a narrow pair may share one), so
  they are encoded rather than learned.
* :class:`FittedResourceModel` — a prior plus a learned residual.  For LUT and FF, which are the
  genuinely estimated counters.
* :class:`InterfaceResourceModel` — a composite's own cost, from **structure** rather than parameters.

Two consequences of the arithmetic, both deliberate:

*Features are not only parameters.*  A composite's own cost depends on how many ``m_axi`` ports and
internal channels it has, at what widths — structural facts, not ``HwParam`` values.  So
:meth:`ResourceModel.features` is handed the elaborated component and takes whatever it needs.  Nothing
is passed down from a parent: elaboration already resolved each child's parameters, so reading them off
the instance cannot drift from what was synthesized.

*Own cost may be negative.*  If HLS shares logic across a module boundary, a composite's own term goes
below zero.  Nothing clamps it — a negative own-cost is the signal that additivity is leaking, and
hiding it would hide precisely what whole-design synthesis exists to catch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from waveflow.calib.confidence import Confidence, ConfidenceLevel
from waveflow.calib.module_key import identify_instance
from waveflow.calib.record_store import RESOURCE_FIELDS

#: Counters a prediction is expressed in.  A design fits only if *every* one fits, so a model that
#: predicts a single aggregate number would be answering the wrong question.
COUNTERS = RESOURCE_FIELDS


def zero_counters() -> dict:
    return {c: 0 for c in COUNTERS}


def add_counters(*terms: dict) -> dict:
    """Sum counter dicts.  Missing counters are 0; **negatives are preserved** (see the module note)."""
    out: dict = {}
    for t in terms:
        for k, v in (t or {}).items():
            if k in COUNTERS:
                out[k] = out.get(k, 0) + int(v)
    return out


@dataclass
class ResourceModel:
    """A module's **own** area — what it costs beyond its children.

    Subclasses implement :meth:`predict_own` and, if they have free parameters, :meth:`fit`.  The base
    handles confidence and the "no free parameters" default, which is the common case here: a lookup
    and a formula both have nothing to fit, so ``fit`` is a no-op *by construction* rather than by an
    exclusion flag.
    """

    #: Human label for reports.  Defaults to the class name.
    name: str = ""

    def __post_init__(self) -> None:
        self.name = self.name or type(self).__name__

    # -- interface ---------------------------------------------------------
    def features(self, comp: Any) -> dict:
        """Whatever this model needs, read off the **elaborated component**.

        Parameters *or* structure: a leaf model typically wants resolved ``HwParam`` values, a
        composite's model wants port and channel counts.  Nothing is threaded down from a parent —
        elaboration already resolved each instance's parameters, so reading them here cannot drift
        from the design that was synthesized.
        """
        return {}

    def predict_own(self, comp: Any) -> dict:
        """This module's own counters, excluding its children."""
        raise NotImplementedError

    def confidence_own(self, comp: Any) -> Confidence:
        """How much :meth:`predict_own` should be believed for *comp*."""
        return Confidence.uncalibrated(f"{self.name} reports no confidence")

    def fit(self, samples) -> "ResourceModel":
        """Fit free parameters from ``[(comp, measured_counters), ...]``.

        The default is a **no-op**, because most models here have no free parameters.  A model whose
        artifact lives in the tracked platform library must additionally not be refit by a
        project-local sweep — that is enforced by where the artifact lives, not by a flag here.
        """
        return self

    @property
    def has_free_params(self) -> bool:
        return False


# ---------------------------------------------------------------------------
# Lookup — the common case
# ---------------------------------------------------------------------------

@dataclass
class LookupResourceModel(ResourceModel):
    """Return the stored measurement for this module's key.

    The right model for a module that does not vary with the knobs being explored — measured across
    the reference sweep, that was three of the four modules.  It has no free parameters and **cannot
    interpolate**: a key it has not seen gets ``UNCALIBRATED``, not a guess.  That refusal is the
    point; a lookup pretending to generalize is how an exploration walks into a region nothing
    measured.
    """

    store: Any = None                      #: a ModuleStore
    target: str = "resource"
    #: Optional in-memory table {module key: counters}, used when there is no store (tests, or a
    #: model carried around without a platform).
    table: dict = field(default_factory=dict)

    def _record(self, comp: Any):
        ident = identify_instance(comp, require_bound=False)
        if ident.key in self.table:
            return ident, self.table[ident.key], "table"
        if self.store is not None:
            rec = self.store.best(ident.key, self.target)
            if rec is not None:
                return ident, rec.payload, rec.source
        return ident, None, None

    def predict_own(self, comp: Any) -> dict:
        _, payload, _ = self._record(comp)
        if payload is None:
            return zero_counters()
        return {c: int(payload[c]) for c in COUNTERS if c in payload}

    def confidence_own(self, comp: Any) -> Confidence:
        ident, payload, source = self._record(comp)
        if payload is None:
            return Confidence(level=ConfidenceLevel.UNCALIBRATED, facts={
                "summary": f"{ident.cls_name}: no measurement stored for key {ident.key}; a lookup "
                           f"cannot interpolate, so this is a gap, not an estimate",
                "module_key": ident.key, "params": ident.params, "model": "lookup"})
        return Confidence(level=ConfidenceLevel.EXACT, facts={
            "summary": f"{ident.cls_name}: measured directly at this configuration",
            "module_key": ident.key, "params": ident.params, "model": "lookup",
            "measured_source": source})


# ---------------------------------------------------------------------------
# Prior — a formula, no fitting
# ---------------------------------------------------------------------------

@dataclass
class PriorResourceModel(ResourceModel):
    """Counters computed from a formula over the module's features.

    For the counters that are *binding decisions* rather than estimates.  DSP and BRAM follow known
    physics — a multiply too wide for the DSP takes two, a narrow pair may share one, an array's
    storage lands in BRAM or in LUTs depending on partitioning — so they are encoded and checked, not
    learned.  A prior that reproduces the corpus with **zero** fitted parameters is a stronger claim
    than any fit, and is reported as such.
    """

    #: ``{counter: callable(features) -> int}``.  Counters absent here are simply not predicted.
    formulas: dict = field(default_factory=dict)
    feature_fn: Any = None                 #: callable(comp) -> dict; defaults to resolved params

    def features(self, comp: Any) -> dict:
        if self.feature_fn is not None:
            return dict(self.feature_fn(comp))
        return dict(identify_instance(comp, require_bound=False).params)

    def predict_own(self, comp: Any) -> dict:
        f = self.features(comp)
        return {c: int(fn(f)) for c, fn in self.formulas.items()}

    def confidence_own(self, comp: Any) -> Confidence:
        ident = identify_instance(comp, require_bound=False)
        return Confidence(level=ConfidenceLevel.EXACT, facts={
            "summary": f"{ident.cls_name}: {', '.join(sorted(self.formulas))} from an analytical "
                       f"prior with no fitted parameters",
            "module_key": ident.key, "model": "prior", "counters": sorted(self.formulas),
            "features": self.features(comp)})
