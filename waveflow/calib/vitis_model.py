"""vitis_model.py — the default resource model for AMD/Xilinx flows.

On this technology the split between what can be *derived* and what must be *fitted* is not a
per-design judgement call.  It follows from the fabric:

* **Hard primitives are allocated, so they are countable.**  A multiply consumes DSPs by the
  device's port geometry; a partitioned array consumes blocks by the block's shape.  Declare how many
  you have and :mod:`waveflow.calib.device_rules` prices them, with **zero** fitted parameters.
* **Soft fabric is what everything else decomposes into, so it is not.**  How much logic a structure
  becomes depends on how the tool shares, retimes and packs.  There is no table to look it up in, so
  LUT and FF are regressed.

:class:`VitisResourceModel` encodes that split once, rather than every design re-deriving it.  A model
author supplies two things and neither is device knowledge:

1. :meth:`VitisResourceModel.structure` — what the design *contains*: multiplier groups and
   partitioned arrays.
2. :meth:`VitisResourceModel.fit_features` + ``fit_basis`` — the basis for the remainder, chosen from
   the structure->form dictionary (see ``docs/examples/vecmult/resource_model.md``).

Both are **overridden methods**, not injected callables: each takes the elaborated component and
describes it, which is what a method is.

WHY THIS AND NOT ``FittedResourceModel(prior=PriorResourceModel(...))``.  That composition works and
is what this replaces, but it names its halves for the *mechanism* (``targets``, ``prior``) rather
than for what they are, so a reader looking for where ``bram`` is predicted has to know that
``predict`` adds the prior in.  Here the halves are ``structure`` and ``fit``, and every counter
is accounted for by name.

AN UNCOVERED COUNTER IS NEVER SILENT.  A model that returned ``{dsp, bram}`` and omitted LUT/FF would
make a design read as *cheaper* than it is -- the one direction an area estimate must not err, since
it turns "does not fit" into "fits".  So counters the model does not cover are reported
``UNCALIBRATED`` by name in :meth:`VitisResourceModel.confidence`.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from waveflow.calib.confidence import Confidence, ConfidenceLevel
from waveflow.calib.device_rules import bram_estimate, dsp_count
from waveflow.calib.module_key import identify_instance
from waveflow.calib.calib import ConcatCalibModel, PriorCalibModel
from waveflow.calib.resource_model import ResourceModel

#: Counters this model derives from declared structure rather than fitting.
#:
#: ``uram`` is here rather than absent because "no declared array asked for UltraRAM" is a
#: *prediction* of zero, not an omission -- and the difference matters: an omitted counter silently
#: contributes nothing, which makes a design read as cheaper than it is.
DERIVED_COUNTERS = ("dsp", "bram", "uram")

#: SRL is not a separate primitive -- it is a LUT in a SLICEM spent as a shift register.  The LUT
#: figures this model is fitted against already account for it however the tool spent them, so it is
#: reported as covered-and-zero rather than as a gap.  A design that wants SRL broken out separately
#: needs a different counter vocabulary, not a different model.
SUBSUMED_COUNTERS = ("srl",)

#: Counters it regresses, when a basis is supplied.
FITTED_COUNTERS = ("lut", "ff")

#: Columns a store-derived corpus carries that are **measurements**, not parameters -- excluded when
#: reconstructing the elaboration parameters for a probe.
RESOURCE_ENRICH_SKIP = DERIVED_COUNTERS + SUBSUMED_COUNTERS + FITTED_COUNTERS


@dataclass(frozen=True)
class MultGroup:
    """``count`` multipliers with ``operand_bits``-wide signed operands.

    A group, not a single multiplier, because a datapath usually replicates one shape -- and because
    a design with two different widths (a coefficient multiply and an address multiply, say) must be
    able to say so rather than averaging them.
    """

    count: int
    operand_bits: int


@dataclass(frozen=True)
class MemArray:
    """One array as the design partitions it.

    ``banks`` is the ``ARRAY_PARTITION`` factor, ``depth`` the entries **per bank**, and
    ``elem_bits`` the element width.  Those three are what decide block cost, and all three are facts
    about the design rather than about the device -- the rounding, the legal block shapes and the
    LUTRAM threshold all live in :func:`~waveflow.calib.device_rules.bram_estimate`.

    An unpartitioned array is ``banks=1`` with the full depth.
    """

    banks: int
    depth: int
    elem_bits: int
    #: Optional label, so a report can say *which* array drove the block count.
    name: str = ""
    #: Bind to UltraRAM rather than block RAM.  Declared by the design because it is a design
    #: decision (an ``HLS BIND_STORAGE`` pragma), not something the device chooses.
    uram: bool = False


@dataclass(frozen=True)
class PerLane:
    """A datapath replicated once per lane -- adders, comparators, enables, muxes of fixed width.

    Contributes the ``n_lane`` basis term.  Not a device rule: how many LUTs a lane of logic becomes
    depends on what the logic is, which is exactly what the fit is for.
    """

    lanes: int
    name: str = ""


@dataclass(frozen=True)
class Crossbar:
    """Any-lane-to-any-position routing: a variable-position mux, a barrel shifter, a crossbar.

    The structure that a **runtime** lane count forces, and the one whose cost surprises people:
    ~``lanes^2`` switches with ``log2(lanes)`` select depth, so it contributes ``xbar_sw`` and
    ``xbar_depth``.  A design whose positions are compile-time constant has none of these.
    """

    lanes: int
    name: str = ""


@dataclass(frozen=True)
class Counter:
    """A counter or address register naming one of ``over`` items.  Contributes ``addr_bits``."""

    over: int
    name: str = ""


@dataclass(frozen=True)
class ReductionTree:
    """A sum/max/and across ``lanes``.  ``lanes - 1`` operators, ``log2(lanes)`` deep.

    Contributes ``reduce_ops``.
    """

    lanes: int
    name: str = ""


@dataclass(frozen=True)
class DesignStructure:
    """What a module contains, in the terms the device rules price.

    Deliberately not "what a module does".  Two designs computing different things from the same
    multiplier groups and the same partitioned arrays cost the same, and a model keyed on intent
    rather than on structure would miss that.
    """

    #: Structures with a device rule -- priced exactly, no fitted parameters.
    multipliers: tuple = ()
    memories: tuple = ()
    #: Structures that become fabric -- they contribute *basis terms* for the LUT/FF regression,
    #: because how much logic a structure becomes is a tool decision with no closed form.
    per_lane: tuple = ()
    crossbars: tuple = ()
    counters: tuple = ()
    reductions: tuple = ()

    _SEQ = ("multipliers", "memories", "per_lane", "crossbars", "counters", "reductions")

    def __post_init__(self) -> None:
        for f in self._SEQ:
            object.__setattr__(self, f, tuple(getattr(self, f)))

    def flatten(self) -> dict:
        """The declaration as **flat scalar columns**, for a corpus row.

        One column per declared structure, enumerated per kind (``xbar0_lanes``, ``xbar1_lanes``).
        Deliberately records the *declaration* and not :meth:`basis_terms`: the terms embed the
        structure->form dictionary, which is a modelling claim that will be revised, and a corpus
        holding revised-away quantities is scrap.  These columns survive any such revision.

        A design that changes its structure changes its columns — which is correct, because it is
        different hardware and its old measurements do not describe it.
        """
        out: dict = {}
        for i, m in enumerate(self.multipliers):
            out[f"mult{i}_count"] = int(m.count)
            out[f"mult{i}_operand_bits"] = int(m.operand_bits)
        for i, m in enumerate(self.memories):
            out[f"mem{i}_banks"] = int(m.banks)
            out[f"mem{i}_depth"] = int(m.depth)
            out[f"mem{i}_elem_bits"] = int(m.elem_bits)
            out[f"mem{i}_uram"] = int(bool(m.uram))
        for kind, rows in (("lane", self.per_lane), ("xbar", self.crossbars),
                           ("reduce", self.reductions)):
            for i, r in enumerate(rows):
                out[f"{kind}{i}_lanes"] = int(r.lanes)
        for i, c in enumerate(self.counters):
            out[f"count{i}_over"] = int(c.over)
        return out

    @staticmethod
    def basis_terms_from(params: dict) -> dict:
        """The fabric basis terms, computed from :meth:`flatten`'s columns.

        Fixed names accumulated across every declared instance, so a design with two crossbars of
        different widths sums them rather than needing the caller to invent a term.  This is the
        structure->form dictionary as arithmetic: **declare the structure once and the basis
        follows**, which is what makes "a bad fit means a missing structure" actionable.

        A ``staticmethod`` over a parameter mapping rather than a method on the object, because this
        must run at fit time from a stored corpus row where no :class:`DesignStructure` exists.  That
        is the whole point of recording :meth:`flatten` rather than the terms.
        """
        def vals(prefix: str, suffix: str) -> list:
            out = []
            i = 0
            while f"{prefix}{i}_{suffix}" in params:
                out.append(float(params[f"{prefix}{i}_{suffix}"]))
                i += 1
            return out

        lanes = vals("lane", "lanes")
        xbars = vals("xbar", "lanes")
        reduces = vals("reduce", "lanes")
        overs = vals("count", "over")
        return {
            "n_lane": float(sum(lanes)),
            "xbar_sw": float(sum(c * c for c in xbars)),
            "xbar_depth": float(sum(c * c * math.log2(c) for c in xbars if c > 1)),
            "addr_bits": float(sum(math.log2(c) for c in overs if c > 1)),
            "reduce_ops": float(sum(max(0.0, r - 1.0) for r in reduces)),
        }

    def basis_terms(self) -> dict:
        """:meth:`basis_terms_from` applied to this structure's own :meth:`flatten`.

        Kept so a structure can still be asked for its terms directly; the two paths are the same
        arithmetic by construction rather than by a parallel implementation.
        """
        return self.basis_terms_from(self.flatten())


@dataclass
class VitisDerived(PriorCalibModel):
    """The **zero-parameter half**: DSP, block RAM and URAM from the recorded structure.

    A :class:`~waveflow.calib.calib.PriorCalibModel` whose formulas are the device rules.  Its inputs
    are the flattened structure columns a corpus row carries (``mult0_count``, ``mem0_banks``, …), so
    it derives its answer from *data*, not from a live component -- which is what lets the same rule
    be replayed against a stored measurement.

    Split out from :class:`VitisResourceModel` in P5 so that "derived" and "fitted" are two composed
    models rather than two code paths inside one, which is what makes the confidence arithmetic
    (weakest-link, per-target) the shared machinery's job rather than this class's.
    """

    part: "str | None" = None

    def _rows(self, params: dict, prefix: str, *fields: str) -> list:
        """Enumerated structure rows back out of the flat columns :meth:`DesignStructure.flatten` wrote."""
        out, i = [], 0
        while f"{prefix}{i}_{fields[0]}" in params:
            out.append(tuple(params[f"{prefix}{i}_{f}"] for f in fields))
            i += 1
        return out

    def _bindings(self, params: dict) -> list:
        """``[(name, BramEstimate), ...]`` — what each array bound to, and how sure the rule is."""
        return [(f"mem{i}", bram_estimate(int(b), int(d), int(e), self.part))
                for i, (b, d, e, _u) in enumerate(
                    self._rows(params, "mem", "banks", "depth", "elem_bits", "uram"))]

    @property
    def targets(self) -> tuple:
        return DERIVED_COUNTERS + SUBSUMED_COUNTERS

    def predict_feat(self, row) -> dict:
        dsp = sum(dsp_count(int(c), int(b), self.part)
                  for c, b in self._rows(row, "mult", "count", "operand_bits"))
        blocks = {True: 0, False: 0}
        for i, (b, d, e, uram) in enumerate(self._rows(row, "mem", "banks", "depth", "elem_bits",
                                                      "uram")):
            blocks[bool(uram)] += bram_estimate(int(b), int(d), int(e), self.part).blocks
        return {"dsp": int(dsp), "bram": int(blocks[False]), "uram": int(blocks[True]), "srl": 0}

    def confidence_feat(self, row) -> Confidence:
        """``EXACT`` unless an array sits where geometry does not decide its binding.

        A prior is normally exact by construction, but the block-vs-LUTRAM threshold has a band the
        device rule declines to resolve.  Inheriting that doubt rather than hiding it is the whole
        reason this overrides :class:`~waveflow.calib.calib.PriorCalibModel`'s flat ``EXACT``.
        """
        bindings = self._bindings(row)
        unsure = [n for n, e in bindings if not e.certain]
        facts: dict = {"model": "vitis-derived", "targets": sorted(self.targets)}
        if bindings:
            facts["memory_binding"] = {n: e.binding for n, e in bindings}
        if unsure:
            facts["summary"] = (f"array(s) {unsure} sit in the band where block-vs-LUTRAM binding is "
                                f"not determined by geometry")
            return Confidence(level=ConfidenceLevel.EXTRAPOLATED, facts=facts)
        facts["summary"] = (f"{', '.join(sorted(self.targets))} from declared structure with no "
                            f"fitted parameters")
        return Confidence(level=ConfidenceLevel.EXACT, facts=facts)


@dataclass
class VitisResourceModel(ConcatCalibModel, ResourceModel):
    """DSP/BRAM from declared structure, LUT/FF from an optional fit.

    The default model kind for an AMD/Xilinx target.  See the module docstring for why the split is
    a property of the technology rather than a choice.
    """

    #: The part the device rules are keyed on.  ``None`` takes it from :attr:`platform`, then from
    #: the device-rule default.
    part: "str | None" = None

    #: ``{counter: [basis-term names]}``.  Empty means "every non-zero term the declaration
    #: produced", which is the intended path -- see :meth:`basis_for`.
    fit_basis: dict = field(default_factory=dict)
    #: Whether LUT/FF are regressed at all.  Set ``False`` only for a module that genuinely has no
    #: fabric to account for, and expect to justify it.
    fit_fabric: bool = True
    #: ``{counter: LinCalibModel}``, built by :meth:`fit` -- the **fabric half**.  Named ``fits``
    #: rather than ``models`` because :attr:`~waveflow.calib.calib.ConcatCalibModel.models` is the
    #: composed tuple, which :meth:`sub_models` assembles from this.
    fits: dict = field(default_factory=dict)

    # -- what a subclass supplies -------------------------------------------
    def structure(self, comp: Any) -> DesignStructure:
        """What *comp* contains — by default, whatever the module itself declares.

        The declaration lives on the **module** (:meth:`~waveflow.hw.hw_module.HwModule.resource_structure`),
        not here, because it is a fact about the design rather than about any model of it: the
        multiplies and the ``ARRAY_PARTITION`` factor are true whether or not anyone estimates
        resources.  Putting it beside the body it describes is what stops the two drifting, and it
        means the common case needs no model subclass at all.

        Override *this* only to price a module that cannot declare its own structure -- a
        third-party component, or a what-if that contradicts the design on purpose.
        """
        fn = getattr(comp, "resource_structure", None)
        if fn is None:
            raise NotImplementedError(
                f"{type(comp).__name__} declares no resource_structure(), and "
                f"{type(self).__name__} does not override structure(); no counter can be derived")
        return fn()

    def fit_features(self, comp: Any) -> dict:
        """Basis terms for the LUT/FF regression.

        Derived from :meth:`structure` by default -- the fabric structures you already declared
        *are* the features, so there is nothing to declare twice and no opportunity for the two
        declarations to disagree.  Override only to add a term the dictionary has no row for.
        """
        return self.structure(comp).basis_terms()

    @property
    def _fits(self) -> bool:
        """Whether this model regresses LUT/FF at all -- :attr:`fit_fabric`.

        Defaults to **True** because on this technology fabric always needs fitting: there is no
        device rule for a LUT.  A model that leaves it True and is never given coefficients reports
        ``UNCALIBRATED`` for LUT/FF, which is the honest outcome -- far better than quietly covering
        only the two counters it can derive and letting a design read as cheaper than it is.
        """
        return bool(self.fit_fabric)

    def basis_for(self, comp: Any, counter: str) -> list:
        """The basis-term names for *counter*: :attr:`fit_basis` if given, else every non-zero
        structure term the declaration produced."""
        named = self.fit_basis.get(counter)
        if named:
            return list(named)
        return [k for k, v in sorted(self.fit_features(comp).items()) if v]

    def declared_counters(self) -> tuple:
        return (DERIVED_COUNTERS + SUBSUMED_COUNTERS
                + (FITTED_COUNTERS if self._fits else ()))

    def _part(self) -> "str | None":
        if self.part is not None:
            return self.part
        return getattr(self.platform, "part", None)

    # -- the derived half ---------------------------------------------------
    def derived(self, comp: Any) -> dict:
        """DSP and BRAM, summed over the declared structure.  No free parameters."""
        s = self.structure(comp)
        part = self._part()
        dsp = sum(dsp_count(g.count, g.operand_bits, part) for g in s.multipliers)
        blocks = {True: 0, False: 0}
        for m in s.memories:
            blocks[bool(m.uram)] += bram_estimate(m.banks, m.depth, m.elem_bits, part).blocks
        return {"dsp": int(dsp), "bram": int(blocks[False]), "uram": int(blocks[True]),
                "srl": 0}

    def memory_bindings(self, comp: Any) -> list:
        """``[(name, BramEstimate), ...]`` -- what each array bound to, and how sure the rule is.

        Exposed because *which* array crossed a threshold is the useful thing to report when a
        prediction looks wrong, and it is invisible in the summed count.
        """
        part = self._part()
        return [(m.name or f"mem{i}", bram_estimate(m.banks, m.depth, m.elem_bits, part))
                for i, m in enumerate(self.structure(comp).memories)]

    # -- the fitted half ----------------------------------------------------
    #: The module class this model prices.  Needed only to **enrich a store-derived corpus**: a
    #: record store holds a module's resolved ``HwParam`` values, and the structure columns the fabric
    #: basis is derived from are not among them.  Structure is a pure function of the parameters
    #: (elaboration's param-purity gate guarantees it), so a probe elaborated at each recorded point
    #: reconstructs exactly what the synthesized design declared -- no synthesis, no second source.
    comp_class: Any = None

    def corpus(self):
        """The store's corpus, with each row's declared structure filled back in.

        Without this the basis terms evaluate to zero on every row and the regression is handed a
        design matrix with no columns -- a failure that looks like "the model predicts nothing"
        rather than "the corpus is missing the features".
        """
        db = super().corpus()
        if db is None or not len(db) or self.comp_class is None:
            return db
        if any(c.startswith("mult0_") or c.startswith("mem0_") for c in db.df.columns):
            return db                      # already carries structure (a corpus.csv, not a store)

        from waveflow.build.elaborate import elaborate

        param_names = [c for c in db.df.columns
                       if c not in ("module_key", "cls_name", "measured_source", "measured_at")
                       and c not in RESOURCE_ENRICH_SKIP]
        extra: dict = {}
        for i, row in enumerate(db.df.to_dict("records")):
            params = {k: int(row[k]) for k in param_names
                      if k in row and str(row[k]).lstrip("-").isdigit()}
            try:
                probe = elaborate(self.comp_class, params, name="probe")
            except Exception:
                continue                   # a row whose params no longer elaborate is not fittable
            for k, v in self.structure(probe).flatten().items():
                extra.setdefault(k, {})[i] = v
        for k, col in extra.items():
            db.df[k] = [col.get(i) for i in range(len(db.df))]
        return db

    def get_params(self, comp: Any, **runtime) -> dict:
        """Resolved ``HwParam`` values **plus a flat record of the declared structure**.

        The structure is what this model actually prices, and it is not recoverable from parameters
        alone by anything that does not hold the component -- so it is recorded here, in
        :meth:`~DesignStructure.flatten`'s scalar columns, and the basis terms are derived from those
        in :meth:`transform`.

        The gain is that the **structure->form dictionary is revisable**.  The cost of a crossbar in
        LUTs is a modelling claim that will be revised; if the corpus stored ``xbar_sw`` the revision
        would strand every measurement, because the lane counts it was computed from would be gone.
        Storing ``xbar0_lanes`` instead means a revised dictionary re-derives from data already on
        disk.
        """
        params = dict(super().get_params(comp, **runtime))
        params.update(self.structure(comp).flatten())
        return params

    def transform(self, params: dict) -> dict:
        """The basis terms for the LUT/FF regression, from the recorded structure columns."""
        return DesignStructure.basis_terms_from(params)

    @property
    def has_free_params(self) -> bool:
        return self._fits

    def load_or_fit(self, path=None, samples=None) -> "VitisResourceModel":
        """Resolve the LUT/FF coefficients: **published artifact first, local corpus second**.

        *path* defaults to :attr:`~waveflow.calib.calib.CalibModel.params_path`, derived from the
        model's name and platform.  With no platform that is ``None``, which is what makes this fall
        through to a local corpus in a toolchain-free test.

        Installing a model and calibrating it are different acts, and conflating them is why
        ``add_rm_self`` used to refit on every elaboration.  The resolution order here is:

        1. **Load** ``path`` if it exists -- a published artifact predicts with no corpus and no
           sklearn, which is the point of having artifacts at all.
        2. **Fit** *samples* otherwise.  Pass a *callable* to defer building the corpus, so the
           common case (an artifact exists) does not pay for elaborating every calibration point.
        3. **Neither** -- the derived half still answers exactly; the fitted half reports
           ``UNCALIBRATED`` rather than returning a seed dressed as a measurement.

        Returns *self*, so it chains off the constructor.
        """
        if not self._fits:
            return self
        path = self.params_path if path is None else path
        if path is not None and Path(path).exists():
            from waveflow.calib.calib import LinCalibModel

            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            for c, blob in payload.get("counters", {}).items():
                m = LinCalibModel(basis=list(blob["basis"]), target=c, name=f"{self.name}.{c}",
                                  transform_fn=DesignStructure.basis_terms_from)
                m.load_params(blob["params"])
                self.fits[c] = m
            return self
        if samples is not None:
            return self.fit(samples() if callable(samples) else samples)

        # No artifact and no samples: fit from the corpus if there is one.  Without this a model
        # given a `store` silently returns unfitted -- every derived counter exact, every fitted one
        # zero -- which reads as a model that predicts almost nothing rather than one that was never
        # asked to fit.
        db = self.corpus()
        if db is not None and len(db):
            return self.fit(db)
        return self

    def save_model(self, path=None) -> "Path":
        """Write the fitted coefficients as an artifact :meth:`load_or_fit` can read back.

        Defaults to :attr:`~waveflow.calib.calib.CalibModel.params_path` -- the same place
        :meth:`load_or_fit` looks, so publishing and loading cannot disagree about where a model
        lives.
        """
        path = self.params_path if path is None else path
        if path is None:
            raise ValueError(
                f"{self.name}: save_model() needs a path, or a platform to derive one from")
        payload = {"model": self.name, "part": self._part(),
                   "counters": {c: {"basis": list(m.basis), "params": m.to_params()}
                                for c, m in self.fits.items()}}
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return out

    def fit(self, samples=None) -> "VitisResourceModel":
        """Fit LUT/FF from a **corpus**, or from ``[(comp, measured_counters), ...]``.

        With nothing passed it reads :meth:`~waveflow.calib.resource_model.ResourceModel.corpus` --
        for a model given a ``store``, the record store reduced on demand.  That is the normal path:
        the measurements are already on disk, addressed by module key, and a fit that re-elaborated
        components to rediscover them would be doing the same work twice from a worse source.

        The sample-pair form remains for a design whose measurements are not in a store yet.  Either
        way the features are recomputed here rather than taken from the caller, so the fit cannot be
        trained on a different feature definition than :meth:`predict` will evaluate.
        """
        if not self._fits:
            return self
        import pandas as pd

        from waveflow.calib.calib import CalibDataFrame, LinCalibModel

        comp0 = None
        if samples is None or isinstance(samples, (pd.DataFrame, CalibDataFrame)):
            df = self._frame(self._fit_data(samples))
        else:
            rows = []
            for comp, measured in samples:
                row = dict(self.get_params(comp))      # raw params -- the corpus row shape
                row.update({c: int(measured[c]) for c in FITTED_COUNTERS if c in measured})
                rows.append(row)
            df = pd.DataFrame(rows)
            comp0 = samples[0][0] if samples else None

        for c in FITTED_COUNTERS:
            if c not in df.columns:
                continue
            if comp0 is not None:
                basis = self.basis_for(comp0, c)
            else:
                # No component to ask, so take the basis from the recorded structure itself -- every
                # term the declaration produces that is not identically zero across the corpus.
                terms = [DesignStructure.basis_terms_from(r) for r in df.to_dict("records")]
                basis = self.fit_basis.get(c) or [k for k in sorted(terms[0]) if any(t[k] for t in terms)]
            self.fits[c] = LinCalibModel(basis=list(basis), target=c, name=f"{self.name}.{c}",
                                         transform_fn=DesignStructure.basis_terms_from).fit(df)
        return self

    # -- composition ---------------------------------------------------------
    def sub_models(self) -> tuple:
        """The derived half plus one regression per fitted counter.

        Assembled from :attr:`fits` rather than stored, so there is no second tuple to fall out of
        step with the dict :meth:`fit` populates.
        """
        return (self._derived_model(),) + tuple(self.fits.values())

    def _derived_model(self) -> VitisDerived:
        return VitisDerived(name=f"{self.name}.derived", part=self._part())

    # -- interface ----------------------------------------------------------
    def predict_feat(self, row) -> dict:
        """Every counter, from a **parameter row** -- the derived ones and the fitted ones.

        Fitted counters are rounded to int here rather than inside the regression: a LUT count is a
        whole number, but the *fit* is over reals and rounding earlier would bias a sum of several.
        """
        out = dict(self._derived_model().predict_feat(row))
        for c, m in self.fits.items():
            out[c] = int(round(float(m.predict_feat(row))))
        return out

    def predict(self, comp: Any, **runtime) -> dict:
        return self.predict_feat(self.get_params(comp, **runtime))

    def uncovered(self, vocabulary=None) -> tuple:
        """Platform counters this model predicts nothing for.

        Never silently zero: a counter outside this model's vocabulary means the platform is not a
        Vitis FPGA target -- an ASIC platform counting cell area, say.  That is exactly when a silent
        zero would be worst, so it downgrades the whole prediction.
        """
        vocab = self.counters() if vocabulary is None else tuple(vocabulary)
        covered = set(self.declared_counters()) | set(self.fits)
        return tuple(c for c in vocab if c not in covered)

    def confidence_feat(self, row) -> Confidence:
        """Weakest-link across the composed halves, then the uncovered-counter downgrade.

        The weakest-link arithmetic is :class:`~waveflow.calib.calib.ConcatCalibModel`'s, not this
        class's -- what remains here is the part that genuinely needs the *platform*: a counter the
        vocabulary has and no sub-model covers.
        """
        conf = ConcatCalibModel.confidence_feat(self, row)
        facts = dict(conf.facts)
        facts["model"] = "vitis"
        facts["derived"] = sorted(DERIVED_COUNTERS)
        level = conf.level

        derived_facts = self._derived_model().confidence_feat(row).facts
        if "memory_binding" in derived_facts:
            facts["memory_binding"] = derived_facts["memory_binding"]
        if self._fits and not self.fits:
            level = ConfidenceLevel.UNCALIBRATED
            facts["summary"] = f"{self.name} has not been fitted"

        missing = self.uncovered()
        if missing:
            level = ConfidenceLevel.UNCALIBRATED
            facts["uncovered"] = list(missing)
            facts["summary"] = (f"{facts.get('summary', '')}; no prediction for {list(missing)} -- "
                                f"their cost is MISSING from this estimate, not zero").lstrip("; ")
        return Confidence(level=level, facts=facts)

    def confidence(self, comp: Any, **runtime) -> Confidence:
        conf = self.confidence_feat(self.get_params(comp, **runtime))
        ident = identify_instance(comp, require_bound=False)
        facts = dict(conf.facts)
        facts["module_key"] = ident.key
        facts["summary"] = f"{ident.cls_name}: {facts.get('summary', '')}"
        return Confidence(level=conf.level, facts=facts)
