"""resource_model.py — predicting a design's resource utilization by composing per-module models.

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
:meth:`ResourceModel.transform` is handed the elaborated component and takes whatever it needs.  Nothing
is passed down from a parent: elaboration already resolved each child's parameters, so reading them off
the instance cannot drift from what was synthesized.

*Own cost may be negative.*  If HLS shares logic across a module boundary, a composite's own term goes
below zero.  Nothing clamps it — a negative own-cost is the signal that additivity is leaking, and
hiding it would hide precisely what whole-design synthesis exists to catch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from waveflow.calib.calib import CalibDataFrame, CalibModel, LookupCalibModel
from waveflow.calib.confidence import Confidence, ConfidenceLevel
from waveflow.calib.module_key import identify_instance
from waveflow.calib.record_store import (INTEGRATION_TARGET, RESOURCE_FIELDS,
                                         normalize_resources)

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
class ResourceModel(CalibModel):
    """A module's **own** utilization — what it costs beyond its children.

    A resource model *is* a :class:`~waveflow.calib.calib.CalibModel` whose targets are the
    platform's counters.  Everything generic — :attr:`name`, :attr:`platform`, the derived storage
    paths, :meth:`transform`, corpus reading — comes from the base; what remains here is the two
    things that are genuinely resource-flavoured: a **counter vocabulary** validated against the
    platform, and a prediction that is a *mapping* rather than a scalar.

    Subclasses implement :meth:`predict` and, if they have free parameters, :meth:`fit`.  The base
    handles confidence and the "no free parameters" default, which is the common case here: a lookup
    and a formula both have nothing to fit, so ``fit`` is a no-op *by construction* rather than by an
    exclusion flag.
    """

    def __post_init__(self) -> None:
        self.name = self.name or type(self).__name__
        self.check_counters(self.declared_counters())

    @property
    def targets(self) -> tuple:
        """Every counter this model predicts — the base's notion of *target*, in resource terms.

        A model that names its counters reports those; one that does not falls back to the platform
        vocabulary.  This is why :meth:`predict` returns a mapping: resource models are the
        multi-target case the base contemplates.
        """
        return self.declared_counters() or self.counters()

    def declared_counters(self) -> tuple:
        """The counters this model claims to predict.  Subclasses that name counters override it."""
        return ()

    def check_counters(self, names) -> None:
        """Refuse a counter the platform does not measure in.

        Without this the vocabulary is advisory: a mistyped counter predicts fine in isolation and is
        dropped when counters are summed, so the module contributes **zero** and nothing complains.
        """
        if self.platform is not None and names:
            self.platform.check_counters(names)

    def counters(self) -> tuple:
        """The platform's counter vocabulary, or the built-in FPGA default."""
        return tuple(self.platform.res_types) if self.platform is not None else COUNTERS

    # -- interface ---------------------------------------------------------
    #: Optional ``callable(comp) -> dict`` overriding :meth:`get_params` -- the **extraction**, for a
    #: model that needs a structural fact no ``HwParam`` records.  Whatever it returns is what the
    #: corpus stores, so keep it raw.
    params_fn: Any = None
    #: The :class:`~waveflow.calib.record_store.ModuleStore` this model's measurements live in.  When
    #: set, it is the corpus source (see :meth:`corpus`).
    store: Any = None
    #: Module class whose records form this model's corpus.  A model serves one class, and two
    #: classes have different parameter names, so an unfiltered corpus is mostly blank columns.
    cls_name: str = ""

    def corpus(self):
        """The record store, **reduced on demand** -- resources derive a corpus, never store one.

        The resource raw tier is a :class:`~waveflow.calib.record_store.ModuleStore` keyed per
        module, not a ``corpus.csv`` keyed per feature point, so this is where the projection happens.
        Regenerating rather than caching is what makes a stale resource corpus impossible: the store
        is the ground truth, and there is no second copy to fall behind it.

        Falls back to the base (a literal ``corpus.csv``) when no store is set, which is what lets a
        test hand a model a frame directly.
        """
        if self.store is None:
            return super().corpus()
        from waveflow.calib.record_store import corpus_from_records

        return corpus_from_records(self.store, cls_name=self.cls_name or None,
                                   counters=self.counters())

    def get_params(self, comp: Any, **runtime) -> dict:
        """The base's identity default, narrowed to what a resource model sees.

        Utilization is fixed at elaboration, so ``**runtime`` is dropped rather than recorded: a
        workload cannot change what was synthesized, and a corpus column that never varies is noise
        a basis could still be selected from by accident.
        """
        if self.params_fn is not None:
            return dict(self.params_fn(comp))
        return dict(identify_instance(comp, require_bound=False).params)

    def predict(self, comp: Any, **runtime) -> dict:
        """This module's own counters, excluding its children.

        The base composition, ``predict_feat`` of :meth:`get_params` — every resource kind now goes
        through it, which is what lets them be composed by
        :class:`~waveflow.calib.calib.ConcatCalibModel` rather than by a bespoke ``prior=`` field.
        """
        return self.predict_feat(self.get_params(comp, **runtime))

    def confidence(self, comp: Any, **runtime) -> Confidence:
        """How much :meth:`predict` should be believed for *comp*."""
        return self.confidence_feat(self.get_params(comp, **runtime))

    def fit(self, samples=None) -> "ResourceModel":
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
class LookupResourceModel(LookupCalibModel, ResourceModel):
    """Look the measurement up for exactly this configuration; refuse to interpolate.

    A :class:`~waveflow.calib.calib.LookupCalibModel` **keyed on the module key**.  Everything that
    makes a lookup a lookup -- memorizing, refusing to interpolate, the ``EXACT``/``UNCALIBRATED``
    transition, the artifact round-trip -- is inherited and identical on both axes.  What is
    specialized is the *identity*, and only that.

    The key is the module's **elaborated structure**, not its parameter tuple, and the two are not
    the same partition.  A bound FIFO depth is physical and reaches the signature, but is no
    ``HwParam``; two instances with identical parameters and different wiring therefore collide under
    a parameter key and stay distinct under this one.  Since a colliding second measurement would
    silently overwrite the first, the finer key is the safe one.

    It is reconciled with the shared machinery by *recording the key as a parameter*
    (:meth:`get_params`), which is exactly what the record-store corpus already emits.  So the
    specialization costs one column rather than a parallel implementation.

    The refusal to interpolate matters more here than on the timing axis: resource laws are full of
    binding thresholds (DSP-vs-LUT inference, block-RAM-vs-LUTRAM partitioning) that make
    interpolation actively wrong rather than merely imprecise.
    """

    target: str = "resource"
    #: Counters this lookup stores, when there is no platform to take a vocabulary from.
    res_types: tuple = ()
    #: Keyed on the module key alone — see the class docstring for why not the parameter tuple.
    basis: list = field(default_factory=lambda: ["module_key"])

    def __post_init__(self) -> None:
        super().__post_init__()
        # A committed table is naturally written {module_key: counters} with a bare string key;
        # internally every key is the tuple `_key` returns.  Normalizing here keeps the ergonomic
        # spelling working rather than silently never matching it.
        self.table = {(k if isinstance(k, tuple) else (str(k),)): v for k, v in self.table.items()}
        self._fitted = bool(self.table)

    def counters(self) -> tuple:
        """:attr:`res_types` if given, else the platform's vocabulary."""
        return tuple(self.res_types) if self.res_types else ResourceModel.counters(self)

    def declared_counters(self) -> tuple:
        return ()          # a lookup returns whatever was measured; it claims no counters up front

    def get_params(self, comp: Any, **runtime) -> dict:
        """Resolved parameters **plus the module key**, which is what this lookup keys on.

        Recording it rather than deriving it is the point: a corpus row then carries the identity its
        measurement was filed under, so the same row serves the fit and the prediction.
        """
        ident = identify_instance(comp, require_bound=False)
        params = dict(ident.params)
        params["module_key"] = ident.key
        params["cls_name"] = ident.cls_name
        return params

    def predict(self, comp: Any, **runtime) -> dict:
        """Through :meth:`predict_feat`, the base path — the only resource kind that uses it."""
        return self.predict_feat(self.get_params(comp, **runtime))

    def confidence(self, comp: Any, **runtime) -> Confidence:
        return self.confidence_feat(self.get_params(comp, **runtime))

    def fit(self, samples=None) -> "LookupResourceModel":
        """Record one table row per measured configuration.

        Accepts either shape: ``[(component, measured_counters), ...]`` -- the resource-side sample
        list -- or a corpus frame, which is what the inherited
        :meth:`~waveflow.calib.calib.LookupCalibModel.fit` reads.  Report spellings (``LUT``,
        ``BRAM_18K``) are normalized on the way in, so a raw synthesis dict is accepted alongside the
        canonical counter names.
        """
        if samples is None or isinstance(samples, (pd.DataFrame, CalibDataFrame)):
            return super().fit(samples)
        for comp, measured in samples:
            row = normalize_resources(measured)
            key = self._key(self.get_params(comp))
            self.table[key] = {c: int(row[c]) for c in self.counters() if c in row}
        self._fitted = bool(self.table)
        return self

    def _record(self, row: dict):
        """The stored measurement for *row*: the fitted table first, the record store second.

        The store fallback is what lets a model predict against a **committed platform library**
        without being fitted at all — the measurements are already on disk, addressed by the same
        key. ``source`` distinguishes the two so a report can say which.
        """
        key = self._key(row)
        if key in self.table:
            return self.table[key], "table"
        mod_key = row.get("module_key")
        if self.store is not None and mod_key:
            rec = self.store.best(str(mod_key), self.target)
            if rec is not None:
                return rec.payload, rec.source
        return None, None

    def predict_feat(self, row):
        payload, _ = self._record(row)
        if payload is None:
            return {c: 0 for c in self.counters()}
        return {c: int(payload[c]) for c in self.counters() if c in payload}

    def confidence_feat(self, row) -> Confidence:
        payload, source = self._record(row)
        cls_name = row.get("cls_name") or self.name
        mod_key = row.get("module_key")
        params = {k: v for k, v in row.items() if k not in ("module_key", "cls_name")}
        if payload is None:
            return Confidence(level=ConfidenceLevel.UNCALIBRATED, facts={
                "summary": f"{cls_name}: no measurement stored for key {mod_key}; a lookup "
                           f"cannot interpolate, so this is a gap, not an estimate",
                "module_key": mod_key, "params": params, "model": "lookup"})
        return Confidence(level=ConfidenceLevel.EXACT, facts={
            "summary": f"{cls_name}: measured directly at this configuration",
            "module_key": mod_key, "params": params, "model": "lookup",
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

    #: ``{counter: callable(inputs) -> int}``.  Counters absent here are simply not predicted.
    formulas: dict = field(default_factory=dict)
    #: Optional ``callable(params) -> dict`` overriding :meth:`ResourceModel.transform` -- the
    #: **derivation**.  Prefer subclassing; this exists for a one-off built inline.
    transform_fn: Any = None

    def declared_counters(self) -> tuple:
        return tuple(self.formulas)

    def transform(self, params: dict) -> dict:
        """:attr:`transform_fn` when given, else the base identity.

        Takes **parameters, not a component** — so a derived basis can only be built from facts
        :meth:`get_params` recorded.  Extraction belongs in :attr:`params_fn`.
        """
        if self.transform_fn is not None:
            return dict(self.transform_fn(params))
        return super().transform(params)

    def predict_feat(self, row) -> dict:
        f = self.transform(row)
        return {c: int(fn(f)) for c, fn in self.formulas.items()}

    def confidence_feat(self, row) -> Confidence:
        return Confidence(level=ConfidenceLevel.EXACT, facts={
            "summary": f"{', '.join(sorted(self.formulas))} from an analytical prior with no "
                       f"fitted parameters",
            "model": "prior", "counters": sorted(self.formulas),
            "inputs": self.transform(row)})

    def confidence(self, comp: Any, **runtime) -> Confidence:
        """The row-based confidence, with the component's identity attached for a report."""
        conf = self.confidence_feat(self.get_params(comp, **runtime))
        ident = identify_instance(comp, require_bound=False)
        facts = dict(conf.facts)
        facts["module_key"] = ident.key
        facts["summary"] = f"{ident.cls_name}: {facts['summary']}"
        return Confidence(level=conf.level, facts=facts)


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

    targets: tuple = ("lut", "ff")                 #: the counters this model fits
    basis: dict = field(default_factory=dict)      #: {counter: [basis-term names]}
    #: Optional ``callable(comp) -> dict`` overriding :meth:`ResourceModel.transform`.
    transform_fn: Any = None
    prior: ResourceModel = None                    #: optional model whose counters are added on top
    models: dict = field(default_factory=dict)     #: {counter: LinCalibModel}, built by fit/load

    def declared_counters(self) -> tuple:
        return tuple(self.targets)

    def transform(self, params: dict) -> dict:
        """:attr:`transform_fn` when given, else the base identity.

        Takes **parameters, not a component** — so a derived basis can only be built from facts
        :meth:`get_params` recorded.  Extraction belongs in :attr:`params_fn`.
        """
        if self.transform_fn is not None:
            return dict(self.transform_fn(params))
        return super().transform(params)

    @property
    def has_free_params(self) -> bool:
        return True

    def fit(self, samples) -> "FittedResourceModel":
        """Fit from ``[(comp, measured_counters), ...]``.

        Features are recomputed from each component rather than taken from the caller, so the fit
        cannot be trained on a different feature definition than :meth:`predict` will evaluate.
        """
        import pandas as pd

        from waveflow.calib.calib import LinCalibModel

        rows = []
        for comp, measured in samples:
            row = dict(self.transform(self.get_params(comp)))
            row.update({c: int(measured[c]) for c in self.targets if c in measured})
            rows.append(row)
        df = pd.DataFrame(rows)

        for c in self.targets:
            if c not in df.columns:
                continue
            basis = list(self.basis.get(c) or [k for k in df.columns if k not in self.targets])
            self.models[c] = LinCalibModel(basis=basis, target=c).fit(df)
        return self

    def predict_feat(self, row) -> dict:
        feats = self.transform(row)
        out = {c: int(round(m.predict_feat(feats))) for c, m in self.models.items()}
        if self.prior is not None:
            out = add_counters(out, self.prior.predict_feat(row))
        return out

    def confidence_feat(self, row) -> Confidence:
        feats = self.transform(row)
        if not self.models:
            return Confidence.uncalibrated(f"{self.name} has not been fitted")
        per = {c: m.confidence_feat(feats) for c, m in self.models.items()}
        detail = "; ".join(f"{c}: {cf.facts.get('summary', cf.level.value)}"
                           for c, cf in sorted(per.items()))
        facts = {"summary": detail, "model": "fitted", "inputs": feats,
                 "per_counter": {c: cf.to_json() for c, cf in per.items()}}
        if self.prior is not None:
            facts["prior"] = self.prior.name
        return Confidence(level=min(cf.level for cf in per.values()), facts=facts)

    def confidence(self, comp: Any, **runtime) -> Confidence:
        conf = self.confidence_feat(self.get_params(comp, **runtime))
        ident = identify_instance(comp, require_bound=False)
        facts = dict(conf.facts)
        facts["module_key"] = ident.key
        facts["summary"] = f"{ident.cls_name}: {facts.get('summary', '')}"
        return Confidence(level=conf.level, facts=facts)


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


def boundary_text(sig) -> str:
    """Canonical text for a boundary signature — the key an interface lookup stores under.

    Text rather than the tuple itself because a lookup key has to survive a CSV round-trip: the same
    boundary written by a live component and read back from a corpus must land on one entry, and a
    nested tuple does not survive that trip while its ``repr`` does.  Sorted on the way in, so port
    order is not part of the identity.
    """
    ports, channels = sig
    return repr((tuple(sorted(tuple(p) for p in ports)),
                 tuple(sorted(tuple(c) for c in channels))))


@dataclass
class InterfaceResourceModel(LookupResourceModel):
    """A composite's **own** cost — the adapters, channel FIFOs, control block and DATAFLOW shell.

    A :class:`LookupResourceModel` **keyed on the boundary** rather than on the module key, and that
    is the entire difference.  Everything that makes a lookup a lookup — memorizing, refusing to
    interpolate, the ``EXACT``/``UNCALIBRATED`` transition, the artifact round-trip — is inherited,
    so what is specialized here is the *identity*, exactly as :class:`LookupResourceModel`
    specializes it away from a parameter tuple.

    Keyed on :func:`boundary_signature` rather than on the composite's parameters because that is
    what the term actually depends on.  The evidence is two-sided: across 24 points varying
    ``ntap``/``samp_w``/realization the term never moved, and it *did* move when ``mem_dwidth``
    changed — identically for both realizations at each width.

    A lookup and not a fit is a statement about the evidence rather than a limitation of ambition.
    A per-port / per-channel decomposition (``Σ adapter_cost(kind, width) + Σ fifo_cost(width,
    depth) + shell``) is the natural next form and is what the signature is shaped to support — but
    separating those coefficients needs more boundary configurations than the two measured so far,
    and fitting them from two points would be inventing structure, not finding it.
    """

    #: Integration records, not resource records: this term is ``top - Σ(modules)``, which is filed
    #: under its own target because it is a different measurement rather than another module's.
    target: str = INTEGRATION_TARGET
    #: The one thing this specializes.  ``LookupResourceModel`` keys on ``module_key``.
    basis: list = field(default_factory=lambda: ["boundary"])

    def __post_init__(self) -> None:
        super().__post_init__()
        # A table is naturally written {boundary_signature: counters}, and internally every key is
        # the one-element tuple `_key` produces.  A signature is a 2-tuple of tuples and an
        # already-keyed entry is a 1-tuple of str, so the two are told apart without a flag.
        self.table = {(k if len(k) == 1 and isinstance(k[0], str) else (boundary_text(k),)): v
                      for k, v in self.table.items()}
        self._fitted = bool(self.table)

    def get_params(self, comp: Any, **runtime) -> dict:
        """The boundary, as parameters — **extraction**, so it is what a corpus would record.

        This model is the clearest case for the params/transform split: its cost depends on ports
        and channels, and **no** ``HwParam`` records either.  Reading them here rather than in
        :meth:`transform` is what puts them in the corpus, and a term whose boundary was never
        recorded could not be re-derived from the measurements it was built from.

        ``ports`` and ``channels`` are kept alongside the key they produce, because the key is a
        modelling choice that may be revised and they are the evidence it was derived from.
        """
        ports, channels = boundary_signature(comp)
        return {"n_ports": len(ports), "n_channels": len(channels),
                "ports": [list(p) for p in ports], "channels": [list(c) for c in channels],
                "boundary": boundary_text((ports, channels))}

    def _key(self, feats) -> tuple:
        """The boundary column when a row carries it, else rebuilt from ``ports``/``channels``.

        Both spellings occur: a live component goes through :meth:`get_params`, while a stored row
        predates the column or was written by the record step.  Rebuilding rather than raising is
        what lets one lookup serve both.
        """
        if "boundary" in feats:
            return (str(feats["boundary"]),)
        return (boundary_text(self._signature(feats)),)

    def _signature(self, row) -> tuple:
        """Rebuild the boundary signature from a recorded row.

        Reconstructed from ``ports`` / ``channels`` rather than re-read from a component, so the
        same lookup serves a live instance and a stored corpus row.
        """
        ports = tuple(sorted(tuple(p) for p in row.get("ports", ())))
        channels = tuple(sorted(tuple(c) for c in row.get("channels", ())))
        return (ports, channels)

    def table_from_store(self) -> dict:
        """Build the boundary -> counters table from the store's **integration** records.

        The composite's own cost is a measurement like any other, and since it is filed it no longer
        has to be transcribed into source.  One record per synthesis, deduplicated here by boundary --
        which is what makes the invariance *derivable*: a 24-point sweep files 24 records, and if they
        agree the table has one entry.

        **A boundary carrying two different measurements raises.**  That is a contradiction in the
        data, not something to average: either the term is not a function of the boundary alone, or two
        different designs were filed against one platform.  Both need a person, and picking one
        quietly would bury the finding the store exists to surface.
        """
        from waveflow.calib.record_store import corpus_from_records

        if self.store is None:
            return {}
        db = corpus_from_records(self.store, cls_name=self.cls_name or None,
                                 target=INTEGRATION_TARGET,
                                 payload_keys=("n_ports", "n_channels", "ports", "channels"))
        table: dict = {}
        for row in db.df.to_dict("records"):
            key = self._key(row)
            counters = {c: int(row[c]) for c in self.counters()
                        if c in row and pd.notna(row[c])}
            prev = table.get(key)
            if prev is not None and prev != counters:
                raise ValueError(
                    f"{self.name}: boundary {key[0]} carries two different integration measurements, "
                    f"{prev} and {counters}. The term is either not a function of the boundary alone, "
                    f"or two designs were filed against one platform.")
            table[key] = counters
        return table

    def load_table(self) -> "InterfaceResourceModel":
        """Fill :attr:`table` from the store when one is set, and return self.

        A no-op without a store, which is what lets a design whose measurements have not been filed
        yet keep supplying a table directly -- see ``plans/integration_record.md`` on why the
        transcribed constants must outlive the reader that replaces them.
        """
        if self.store is not None:
            self.table = self.table_from_store()
            self._fitted = bool(self.table)
        return self

    def confidence_feat(self, row) -> Confidence:
        """The inherited EXACT/UNCALIBRATED split, worded for a boundary rather than a module key."""
        payload, source = self._record(row)
        ports, channels = self._signature(row)
        n_p = int(row.get("n_ports", len(ports)))
        n_c = int(row.get("n_channels", len(channels)))
        facts = {"model": "interface", "n_ports": n_p, "n_channels": n_c,
                 "boundary": self._key(row)[0]}
        if payload is None:
            return Confidence(level=ConfidenceLevel.UNCALIBRATED, facts={
                "summary": f"no interface measurement for this boundary "
                           f"({n_p} port(s), {n_c} channel(s)); the term is a lookup over measured "
                           f"boundaries and cannot interpolate", **facts})
        return Confidence(level=ConfidenceLevel.EXACT, facts={
            "summary": f"interface measured directly for this boundary "
                       f"({n_p} port(s), {n_c} channel(s))",
            "measured_source": source, **facts})


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


def compose(top: Any, model_for=None) -> ResourceEstimate:
    """Predict *top*'s total area by walking its graph: own model + Σ children, recursively.

    By default each module supplies its **own** model — whatever
    :meth:`~waveflow.hw.hw_module.HwModule.add_rm` installed. Call ``top.add_rm(platform)`` once and
    the whole hierarchy is modelled; there is no registry to keep in step with the design.

    *model_for* overrides that with a callable mapping component to model, for a case the attached
    models do not cover (a what-if, a test). Either way a module with no model contributes nothing and
    is reported ``UNCALIBRATED`` rather than silently skipped — a missing contribution makes a design
    read as *cheaper* than it is.

    Nothing is passed down: each model reads its own features off the instance it is attached to,
    because elaboration already resolved every child's parameters.
    """
    from waveflow.calib.module_key import walk_modules

    if model_for is None:
        def model_for(comp):
            return getattr(comp, "resource_model", None)

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
        res = model.predict(comp)
        per_module.append((path, ident.cls_name, res, model.confidence(comp)))
        total = add_counters(total, res)
        if comp is top:
            own = dict(res)
    return ResourceEstimate(total=total, per_module=per_module, own=own)
