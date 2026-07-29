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


# ---------------------------------------------------------------------------
# Fitted — the genuinely learned part
# ---------------------------------------------------------------------------

@dataclass
class FittedResourceModel(ResourceModel):
    """Counters regressed from physically-motivated features — for LUT and FF.

    The counters a prior cannot reach.  DSP and BRAM are binding decisions HLS reports; LUT and FF are
    the *estimate*, absorbing partitioned storage, pipeline registers, the accumulator tree, address
    and mux logic, and whatever multiply residue the DSPs did not take.  There is no closed form for
    that, so it is fit — but from features chosen for physical meaning (storage bits, multiplier count,
    accumulator width), not from raw parameters, so the fit extrapolates on structure rather than on
    coincidence.

    One :class:`~waveflow.calib.calib.LinCalibModel` per counter, because the counters have different
    forms and a single multi-target fit would be wrong.  Confidence comes straight from the underlying
    model's retained :class:`~waveflow.calib.confidence.FitSummary`, so an out-of-range query is
    reported as extrapolation here exactly as it is for a timing fit.
    """

    counters: tuple = ("lut", "ff")
    basis: dict = field(default_factory=dict)      #: {counter: [feature names]}
    feature_fn: Any = None                         #: callable(comp) -> dict of features
    prior: ResourceModel = None                    #: optional model whose counters are added on top
    models: dict = field(default_factory=dict)     #: {counter: LinCalibModel}, built by fit/load

    def features(self, comp: Any) -> dict:
        if self.feature_fn is None:
            return dict(identify_instance(comp, require_bound=False).params)
        return dict(self.feature_fn(comp))

    @property
    def has_free_params(self) -> bool:
        return True

    def fit(self, samples) -> "FittedResourceModel":
        """Fit from ``[(comp, measured_counters), ...]``.

        Features are recomputed from each component rather than taken from the caller, so the fit
        cannot be trained on a different feature definition than :meth:`predict_own` will evaluate.
        """
        import pandas as pd

        from waveflow.calib.calib import LinCalibModel

        rows = []
        for comp, measured in samples:
            row = dict(self.features(comp))
            row.update({c: int(measured[c]) for c in self.counters if c in measured})
            rows.append(row)
        df = pd.DataFrame(rows)

        for c in self.counters:
            if c not in df.columns:
                continue
            basis = list(self.basis.get(c) or [k for k in df.columns if k not in self.counters])
            self.models[c] = LinCalibModel(basis=basis, target=c).fit(df)
        return self

    def predict_own(self, comp: Any) -> dict:
        row = self.features(comp)
        out = {c: int(round(m.predict(row))) for c, m in self.models.items()}
        if self.prior is not None:
            out = add_counters(out, self.prior.predict_own(comp))
        return out

    def confidence_own(self, comp: Any) -> Confidence:
        ident = identify_instance(comp, require_bound=False)
        row = self.features(comp)
        if not self.models:
            return Confidence.uncalibrated(
                f"{ident.cls_name}: {self.name} has not been fitted")

        per = {c: m.confidence(row) for c, m in self.models.items()}
        worst = min(cf.level for cf in per.values())
        detail = "; ".join(f"{c}: {cf.facts.get('summary', cf.level.value)}"
                           for c, cf in sorted(per.items()))
        facts = {
            "summary": f"{ident.cls_name}: {detail}",
            "module_key": ident.key, "model": "fitted", "features": row,
            "per_counter": {c: cf.to_json() for c, cf in per.items()},
        }
        if self.prior is not None:
            facts["prior"] = self.prior.name
        return Confidence(level=worst, facts=facts)


# ---------------------------------------------------------------------------
# Interface — a composite's own cost, from structure
# ---------------------------------------------------------------------------

def boundary_signature(comp: Any) -> tuple:
    """A canonical descriptor of *comp*'s interface graph: its external ports and internal channels.

    This is what a composite's own cost is a function of.  Measured on ``examples/fir_block``, the
    term was **identical** across a 24-point sweep of the compute parameters and **changed** when the
    memory word width changed — because the first moves nothing here and the second re-widens every
    adapter and FIFO.

    Deliberately coarse: port *kinds* and widths, and channel widths, sorted.  Names and order are
    context, not structure, so two designs with the same shape of boundary share a signature.
    """
    ports = []
    for entry in (getattr(comp, "boundary", ()) or ()):
        try:
            ep = entry[1]                  # the port NAME is context, not structure
        except (TypeError, IndexError):
            continue
        kind = type(ep).__name__
        width = getattr(ep, "bitwidth", None) or getattr(ep, "mem_dwidth", None) or 0
        ports.append((kind, int(width)))

    channels = []
    for iface in (getattr(comp, "interfaces", {}) or {}).values():
        width = getattr(iface, "bitwidth", None) or getattr(iface, "mem_dwidth", None) or 0
        channels.append((int(width), int(getattr(iface, "depth", 0) or 0)))

    return (tuple(sorted(ports)), tuple(sorted(channels)))


@dataclass
class InterfaceResourceModel(ResourceModel):
    """A composite's **own** cost — the adapters, channel FIFOs, control block and DATAFLOW shell.

    Keyed on :func:`boundary_signature` rather than on the composite's parameters, because that is
    what the term actually depends on.  The evidence is two-sided: across 24 points varying
    ``ntap``/``samp_w``/realization the term never moved, and it *did* move when ``mem_dwidth``
    changed — identically for both realizations at each width.

    It is a **lookup**, not a fit, and that is a statement about the evidence rather than a
    limitation of ambition.  A per-port / per-channel decomposition (``Σ adapter_cost(kind, width) +
    Σ fifo_cost(width, depth) + shell``) is the natural next form and is what the signature is shaped
    to support — but separating those coefficients needs more boundary configurations than the two
    measured so far, and fitting them from two points would be inventing structure, not finding it.
    """

    table: dict = field(default_factory=dict)      #: {boundary_signature: counters}

    def features(self, comp: Any) -> dict:
        ports, channels = boundary_signature(comp)
        return {"n_ports": len(ports), "n_channels": len(channels),
                "ports": [list(p) for p in ports], "channels": [list(c) for c in channels]}

    def predict_own(self, comp: Any) -> dict:
        got = self.table.get(boundary_signature(comp))
        return dict(got) if got else zero_counters()

    def confidence_own(self, comp: Any) -> Confidence:
        sig = boundary_signature(comp)
        f = self.features(comp)
        if sig not in self.table:
            return Confidence(level=ConfidenceLevel.UNCALIBRATED, facts={
                "summary": f"no interface measurement for this boundary "
                           f"({f['n_ports']} port(s), {f['n_channels']} channel(s)); the term is a "
                           f"lookup over measured boundaries and cannot interpolate",
                "model": "interface", **f})
        return Confidence(level=ConfidenceLevel.EXACT, facts={
            "summary": f"interface measured directly for this boundary "
                       f"({f['n_ports']} port(s), {f['n_channels']} channel(s))",
            "model": "interface", **f})


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResourceEstimate:
    """A composed prediction, with the breakdown and the weakest confidence that fed it."""

    total: dict
    per_module: list = field(default_factory=list)   #: [(path, cls_name, counters, Confidence)]
    own: dict = field(default_factory=dict)          #: the top's own (interface) term

    @property
    def level(self) -> ConfidenceLevel:
        """The **weakest** link — a composed estimate is only as good as its worst part."""
        levels = [c.level for _, _, _, c in self.per_module]
        return min(levels) if levels else ConfidenceLevel.UNCALIBRATED

    def weakest(self) -> list:
        """The modules at the weakest level — what an agent would recalibrate first."""
        worst = self.level
        return [(p, n, c) for p, n, _, c in self.per_module if c.level is worst]

    def to_json(self) -> dict:
        return {"total": dict(self.total), "own": dict(self.own), "level": self.level.value,
                "per_module": [{"path": p, "cls_name": n, "resources": dict(r),
                                "confidence": c.to_json()} for p, n, r, c in self.per_module]}


def compose(top: Any, model_for) -> ResourceEstimate:
    """Predict *top*'s total area by walking its graph: own model + Σ children, recursively.

    *model_for* maps a component to its :class:`ResourceModel` (or ``None`` — a module with no model
    contributes nothing and is reported ``UNCALIBRATED``, rather than being silently skipped).

    Nothing is passed down: each model reads its own features off the instance it is attached to,
    because elaboration already resolved every child's parameters.
    """
    from waveflow.calib.module_key import walk_modules

    per_module, total = [], zero_counters()
    own: dict = {}
    for path, comp, ident in walk_modules(top, include_top=True):
        model = model_for(comp)
        if model is None:
            per_module.append((path, ident.cls_name, zero_counters(),
                               Confidence.uncalibrated(
                                   f"{ident.cls_name} has no resource model; its cost is missing "
                                   f"from this estimate, not zero")))
            continue
        res = model.predict_own(comp)
        per_module.append((path, ident.cls_name, res, model.confidence_own(comp)))
        total = add_counters(total, res)
        if comp is top:
            own = dict(res)
    return ResourceEstimate(total=total, per_module=per_module, own=own)
