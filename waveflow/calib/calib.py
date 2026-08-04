"""calib.py — bare-bones calibration corpus + model fitting.

The pieces a timing calibration recurs on (FIR, VMAC, every future cycle model):

* :class:`CalibDataFrame` — a thin wrapper composing a ``pandas.DataFrame`` (``.df``),
  one row per synth/cosim measurement.  This *is* the structured timing-artifact corpus.
  It adds only what a raw frame lacks: a storage path (:meth:`save` / :meth:`load`) and a
  per-row ``measured_at`` timestamp on :meth:`add_datapoint`.  Everything else is native
  pandas — filter/select/display via ``db.df`` (``db.df[db.df.n_row == 4]``, ``db.df["nwords"]``).
* :class:`CalibModel` — the fit / predict / score interface.  Per-target (one model
  per ``read_span`` / ``write_span`` / ``compute`` / ``fill``): targets have
  different forms, so a single multi-target fit is wrong.  Models take **column-name
  strings** for ``basis`` / ``target``; the design matrix is ``df[basis].to_numpy(float)``.
  Any transform is a caller-side derived column (``df["sqrt_nc"] = df.n_col ** 0.5``).
* :class:`LinCalibModel` — ``sklearn.LinearRegression`` over the basis columns, with
  held-out error, R² and a simple plot.
* :class:`InterpCalibModel` — a calibrated 1-D lookup (a measured table, not a curve fit).

Intentionally minimal: a DataFrame wrapper + sklearn / interp models, not an ML framework.
"""
from __future__ import annotations

import datetime as _datetime
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from waveflow.calib.confidence import Confidence, ConfidenceLevel, Estimate, FitSummary

#: Metadata column stamped on every row by :meth:`CalibDataFrame.add_datapoint`.
#: It is *never* a feature/target — models select explicit basis/target columns, so it
#: cannot leak into a fit.
MEASURED_AT = "measured_at"


def _now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# CalibDataFrame — the structured datapoint corpus (a thin DataFrame wrapper)
# ---------------------------------------------------------------------------

@dataclass
class CalibDataFrame:
    """A table of calibration datapoints backed by a ``pandas.DataFrame`` (:attr:`df`).

    Construct empty (optionally declaring a *columns* order) and append rows with
    :meth:`add_datapoint` / :meth:`extend`; each row is stamped with a ``measured_at``
    timestamp.  Filter / select / display with native pandas on :attr:`df`.  Persist with
    :meth:`save` (csv) and reload with :meth:`load`.
    """

    columns: Sequence[str] | None = None
    path: str | Path | None = None
    df: pd.DataFrame = field(init=False)

    def __post_init__(self) -> None:
        self._declared = list(self.columns) if self.columns else []
        self.df = pd.DataFrame(columns=self._declared)

    # -- ordering: declared columns first, extras next, measured_at last ----
    def _order(self, cols: Iterable[str]) -> list[str]:
        cols = list(cols)
        declared = [c for c in self._declared if c in cols]
        extra = [c for c in cols if c not in declared and c != MEASURED_AT]
        tail = [MEASURED_AT] if MEASURED_AT in cols else []
        return declared + extra + tail

    def add_datapoint(self, point: dict, *, measured_at: str | None = None) -> None:
        """Append one datapoint (a mapping of column -> value), timestamped."""
        row = dict(point)
        row[MEASURED_AT] = _now() if measured_at is None else measured_at
        new = pd.DataFrame([row])
        combined = new if self.df.empty else pd.concat([self.df, new], ignore_index=True)
        self.df = combined.reindex(columns=self._order(combined.columns))

    def extend(self, points: Iterable[dict]) -> None:
        for p in points:
            self.add_datapoint(p)

    def __len__(self) -> int:
        return len(self.df)

    # -- storage ------------------------------------------------------------
    def save(self, path: str | Path | None = None) -> Path:
        """Write the corpus to *path* (or :attr:`path`) as csv; returns the path."""
        p = path if path is not None else self.path
        if p is None:
            raise ValueError("no path given to save() and CalibDataFrame.path is unset")
        p = Path(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.df.to_csv(p, index=False)
        return p

    @classmethod
    def load(cls, path: str | Path) -> "CalibDataFrame":
        """Load a corpus previously written by :meth:`save`."""
        obj = cls(path=path)
        obj.df = pd.read_csv(path)
        return obj


# ---------------------------------------------------------------------------
# CalibModel — per-target fit / predict / score
# ---------------------------------------------------------------------------

@dataclass
class CalibModel:
    """Base per-target calibration model: predict ``target`` from a ``basis`` of columns.

    ``basis`` / ``target`` are **column-name strings**; the design matrix is
    ``df[basis].to_numpy(float)`` and targets are ``df[target].to_numpy(float)``.
    Subclasses implement :meth:`fit` and :meth:`_predict_one`.  One model per target —
    ``read_span``, ``write_span``, ``compute``, ``fill`` each get their own (different
    forms; a single multi-target fit would be wrong).

    Parameters are a **named dict** (the ``state_dict`` — cf. torch weights): :meth:`to_params`
    /:meth:`load_params` are the per-model payload, and the base owns the generic JSON artifact I/O
    (:meth:`save_model` / :meth:`load_model`) + the :attr:`seed` fallback (:meth:`default_model` /
    :meth:`load_or_default`).  The model *shell* (basis / transform / names) is built in code; only
    the numbers live in the artifact at :attr:`path` (which the BuildDAG tracks)."""

    #: Column names forming the design matrix.  Empty for a model that does not predict from a flat
    #: feature vector at all -- a lookup keyed on a module, or a formula reading structure.
    basis: list[str] = field(default_factory=list)
    #: The single target this model predicts.  Empty for a multi-target model, which overrides
    #: :attr:`targets` instead.
    target: str = ""
    seed: dict | None = None
    path: "str | Path | None" = None
    #: Identifies this model, and names the directory its data lives in.  Defaults to :attr:`target`.
    name: str = ""
    #: The platform this model is calibrated for.  Supplies the storage root, and (for a resource
    #: model) the counter vocabulary.  ``None`` is legal: the model then has no derived paths, which
    #: is what lets a test build one with no platform at all.
    platform: "Any" = None

    #: Multiple targets, when this model predicts more than one.  Named ``target_names`` rather than
    #: ``targets`` because :attr:`targets` is the derived property every caller reads -- a field and a
    #: property cannot share a name.  Leave empty for the single-target case.
    target_names: "tuple[str, ...]" = ()

    @property
    def targets(self) -> "tuple[str, ...]":
        """Every target this model predicts.

        Single-target models -- the common case -- report ``(target,)`` and :meth:`predict_feat`
        returns a scalar.  A model given :attr:`target_names`, or a subclass that derives them (a
        resource model's counters), reports several, and :meth:`predict_feat` returns a **mapping**
        instead -- the same convention scikit-learn uses for multi-output estimators.
        """
        if self.target_names:
            return tuple(self.target_names)
        return (self.target,) if self.target else ()

    # -- where this model's data lives (P1 of plans/harmonize_calib.md) ------
    @property
    def data_dir(self) -> "Path | None":
        """``<platform.dir>/models/<name>/``, or ``None`` with no platform.

        Derived rather than passed.  Before this, three separate schemes hand-rolled the same
        decision: ``TimingModel.corpus_path``, ``CalibModel.path`` and a ``platform_artifact``
        helper on the resource side.
        """
        root = getattr(self.platform, "dir", None)
        return Path(root) / "models" / (self.name or self.target) if root else None

    @property
    def corpus_path(self) -> "Path | None":
        """Where the measured points live."""
        d = self.data_dir
        return d / "corpus.csv" if d else None

    @property
    def params_path(self) -> "Path | None":
        """Where the fitted parameters live -- the artifact :meth:`load_model` reads."""
        d = self.data_dir
        return d / "params.json" if d else None

    # -- reading the corpus -------------------------------------------------
    def corpus(self) -> "CalibDataFrame | None":
        """Load :attr:`corpus_path`, or ``None`` if there is none on disk.

        The corpus is a **view** of measurements that live in an axis-specific raw tier (RTL runs,
        synthesis records).  Nothing here writes it; each axis regenerates it from its own raw tier,
        which is what makes a stale corpus impossible.
        """
        p = self.corpus_path
        return CalibDataFrame.load(p) if p and Path(p).is_file() else None

    @property
    def corpus_df(self) -> "pd.DataFrame | None":
        """The corpus as a ``pandas.DataFrame`` -- the working form for analysis."""
        c = self.corpus()
        return None if c is None else c.df

    def corpus_markdown(self, max_rows: "int | None" = None) -> str:
        """The corpus as a markdown table.

        A corpus is *evidence*: the numbers behind a model belong in the document that makes a claim
        about it, and in the context of anything reasoning about the design.  ``max_rows`` truncates
        with a visible note rather than silently -- a table that quietly dropped rows would misstate
        how much a model was fitted on.
        """
        df = self.corpus_df
        if df is None or df.empty:
            return f"*(no corpus at `{self.corpus_path}`)*"
        shown, dropped = df, 0
        if max_rows is not None and len(df) > max_rows:
            shown, dropped = df.head(max_rows), len(df) - max_rows
        head = "| " + " | ".join(str(c) for c in shown.columns) + " |"
        rule = "|" + "|".join("---" for _ in shown.columns) + "|"
        rows = ["| " + " | ".join(str(v) for v in r) + " |" for r in shown.itertuples(index=False)]
        out = "\n".join([head, rule, *rows])
        return out + (f"\n\n*({dropped} further row(s) not shown)*" if dropped else "")

    def corpus_row(self, comp, measured: dict, **runtime) -> dict:
        """One corpus row: :meth:`get_params` plus the measured target(s).

        The single place a row is assembled, so what is *recorded* and what is later *read back* by
        :meth:`transform` cannot drift apart -- which is the whole reason a corpus can be re-fitted
        years later against a model whose derivation has changed since.
        """
        row = self.get_params(comp, **runtime)
        row.update({t: measured[t] for t in self.targets if t in measured})
        return row

    def _fit_data(self, data):
        """Resolve what :meth:`fit` should train on: explicit *data*, else the corpus.

        Passing data is deliberately the exception.  A fit that bypassed the recorded corpus would
        leave parameters with no answer to "fitted on what?", so the default path reads what was
        actually measured.
        """
        if data is not None:
            return data
        df = self.corpus_df
        if df is None or df.empty:
            raise RuntimeError(
                f"{type(self).__name__}({self.name!r}): fit() was given no data and there is no "
                f"corpus to read"
                + (f" at {self.corpus_path}" if self.corpus_path else " (no platform is set)"))
        return df

    def __post_init__(self) -> None:
        self.basis = [str(b) for b in self.basis]
        self.target = str(self.target)
        self.name = str(self.name) or self.target
        self._fitted = False
        self._fit_summary = None       # FitSummary — set by _record_fit_summary at the end of fit()
        self._from_seed = False        # True when the params came from `seed`, not from data

    # -- data access --------------------------------------------------------
    @staticmethod
    def _frame(data) -> pd.DataFrame:
        """Accept a :class:`CalibDataFrame` or a raw ``DataFrame``; return the frame."""
        return data.df if isinstance(data, CalibDataFrame) else data

    def design(self, df: pd.DataFrame) -> np.ndarray:
        return df[self.basis].to_numpy(dtype=float)

    def target_column(self, df: pd.DataFrame) -> np.ndarray:
        return df[self.target].to_numpy(dtype=float)

    # -- interface (subclass) ----------------------------------------------
    def fit(self, data=None) -> "CalibModel":
        """Calibrate from measurements and return **self**, scikit-learn style.

        *data* is optional: the normal path reads the model's own :attr:`corpus_path`, and passing a
        frame is the explicit override for a test or a one-off.  Subclasses resolve it through
        :meth:`_fit_data`.
        """
        raise NotImplementedError

    def _predict_one(self, row) -> float:
        raise NotImplementedError

    def predict_feat(self, feats) -> float:
        """Predict from a **feature mapping** — the numeric layer.

        Named for the scikit-learn-ish split this class now has: :meth:`transform` turns a component
        into features, this turns features into a number, and :meth:`predict` is the two composed.
        A caller that already holds features calls this directly.
        """
        if not self._fitted:
            raise RuntimeError(f"{type(self).__name__}({self.target!r}) is not fitted")
        return self._predict_one(feats)

    # -- the component-facing layer (P1 of plans/harmonize_calib.md) ---------
    def get_params(self, comp, **runtime) -> dict:
        """Everything about *comp* this model needs — and **exactly what a corpus row records**.

        The **default is the identity**: every resolved ``HwParam``, by name, plus ``**runtime``
        (workload inputs that are not properties of the design — a timing model's ``nwords``).

        Override it to add facts that shape the hardware but are **not** ``HwParam`` values: a bound
        FIFO depth, a port count, the width of an internal channel.  ``HwParam`` is what a module
        *declares*; a synthesized circuit also depends on how it was *wired*, and a model blind to
        that predicts the same number for two different circuits.

        The rule for what belongs here is **rawness, not usefulness**: record the measured facts, not
        a quantity derived from them.  A model that predicts from ``a*b*c`` should record ``a``, ``b``
        and ``c`` -- derive the product in :meth:`transform`.  Storing the product instead makes the
        corpus unusable the moment the derivation changes, because the inputs it was computed from are
        gone.  A corpus outlives the model fitted from it; a derived column does not.
        """
        from waveflow.calib.module_key import identify_instance

        params = dict(identify_instance(comp, require_bound=False).params)
        params.update(runtime)
        return params

    def transform(self, params: dict) -> dict:
        """Parameters -> the features a prediction is computed from.  Default: the identity.

        Takes a **parameter mapping, never a component**, and that restriction *is* the design.  It
        makes it structurally impossible for a model to predict from something :meth:`get_params`
        did not extract — and therefore from something the corpus did not record.  A model whose
        prediction depended on a fact absent from its corpus could never be honestly re-fitted, and
        nothing would say so; here it cannot be written in the first place.

        The consequence is that :meth:`fit` and :meth:`predict` run the *same* function over the
        *same* shape — a stored corpus row and ``get_params(comp)`` are interchangeable inputs — so
        they cannot disagree about what a feature means.  There is only one definition of one.

        Override for a **derived** quantity, and leave :meth:`get_params` alone so the corpus keeps
        the raw facts the derivation is computed from::

            def transform(self, params):
                return {"area": params["a"] * params["b"] * params["c"]}
        """
        return dict(params)

    def predict(self, comp, **runtime) -> float:
        """:meth:`predict_feat` of :meth:`get_params` — the component-facing entry.

        Through :meth:`get_params`, **not** :meth:`transform`: what a component contributes is a
        parameter row, which is exactly the shape a corpus stores.  :meth:`transform` is applied
        inside :meth:`predict_feat`, so predicting from a live component and predicting from a
        recorded row take the identical path.
        """
        return self.predict_feat(self.get_params(comp, **runtime))

    # -- metrics ------------------------------------------------------------
    def score(self, data) -> float:
        """R² of the fitted model on *data* (a CalibDataFrame or DataFrame)."""
        df = self._frame(data)
        y = self.target_column(df)
        pred = np.array([self.predict_feat(r) for r in df.to_dict("records")], dtype=float)
        ss_res = float(np.sum((y - pred) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        return 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0

    def rel_errors(self, data) -> list[float]:
        """Per-row ``|pred - actual| / |actual|`` on *data* (skips actual==0)."""
        out = []
        for r in self._frame(data).to_dict("records"):
            actual = float(r[self.target])
            if actual == 0:
                continue
            out.append(abs(self.predict_feat(r) - actual) / abs(actual))
        return out

    def max_rel_error(self, data) -> float:
        errs = self.rel_errors(data)
        return max(errs) if errs else 0.0

    # -- confidence ---------------------------------------------------------
    @property
    def has_free_params(self) -> bool:
        """Whether fitting this model can change what it predicts.

        ``True`` by default, because most forms carry coefficients.  A formula overrides it to
        ``False``, which is what lets :class:`ConcatCalibModel` skip it when counting the parameters
        behind an estimate — and what makes "reproduces every point with **zero** free parameters" a
        claim the machinery can check rather than one a docstring asserts.
        """
        return True

    def uncovered(self, vocabulary=None) -> "tuple[str, ...]":
        """Targets in *vocabulary* this model predicts **nothing** for.

        Under-counting is the one direction an estimate must not err: a target silently absent
        contributes zero, which turns *"does not fit"* into *"fits"*.  Naming the gap is what lets
        :meth:`confidence_feat` downgrade instead of a caller discovering it from a suspiciously
        small number.

        *vocabulary* defaults to :attr:`targets`, for which the answer is trivially empty; the useful
        call passes a wider set — a platform's counters, say — and asks what is missing from it.
        """
        vocab = tuple(self.targets) if vocabulary is None else tuple(vocabulary)
        mine = set(self.targets)
        return tuple(t for t in vocab if t not in mine)

    def n_free_params(self) -> int:
        """How many parameters this model form is free to choose.

        Used only to keep an exactness claim honest: zero residual over no more points than free
        parameters is guaranteed by construction and says nothing.  The default assumes one
        coefficient per basis feature plus an intercept; subclasses whose form differs override it.
        """
        return len(self.basis) + 1

    def _record_fit_summary(self, data) -> "FitSummary":
        """Capture the little a *deployed* model needs to report confidence, and return it.

        Called at the end of :meth:`fit`.  A fitted model discards its training data — that is what
        lets a published artifact predict with no sklearn and no corpus — so the support region and
        the worst residual have to be retained explicitly or confidence becomes unanswerable in
        exactly the place it matters most: a platform library reused by a project that never ran the
        sweep.

        Ranges are recorded over the frame's **raw numeric columns** rather than :attr:`basis`,
        because queries arrive in raw form (``{"nwords": 128}``) even when the model's basis is a
        derived transform of them.
        """
        df = self._frame(data)
        targets = self.targets
        ranges: dict = {}
        for col in df.columns:
            if col in targets:
                continue
            series = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(series):
                ranges[str(col)] = [float(series.min()), float(series.max())]

        # Multi-target models residual over **every** target and keep the worst.  Taking the mean
        # would let one exactly-reproduced counter mask another that the form does not fit, and the
        # whole point of the summary is that `EXACT` is a checked claim rather than an assertion.
        abs_err, rel_err = 0.0, 0.0
        for row in df.to_dict("records"):
            pred = self.predict_feat(row)
            for t in targets:
                if t not in row or pd.isna(row[t]):
                    continue
                actual = float(row[t])
                got = pred[t] if isinstance(pred, dict) else pred
                resid = abs(float(got) - actual)
                abs_err = max(abs_err, resid)
                if actual:
                    rel_err = max(rel_err, resid / abs(actual))

        self._fit_summary = FitSummary(
            features=list(self.basis), ranges=ranges, n_points=int(len(df)),
            max_abs_residual=abs_err, max_rel_residual=rel_err,
            n_free_params=int(self.n_free_params()))
        self._from_seed = False
        return self._fit_summary

    def confidence(self, comp, **runtime) -> Confidence:
        """:meth:`confidence_feat` of :meth:`transform` — the component-facing entry."""
        return self.confidence_feat(self.get_params(comp, **runtime))

    def confidence_feat(self, row) -> Confidence:
        """How much :meth:`predict` should be believed for *row*.

        The level is **derived** from the retained fit summary rather than asserted, so a model cannot
        claim ``EXACT`` while carrying an out-of-range query or an under-determined fit.  Facts are
        free-form and model-specific; subclasses extend them via :meth:`_confidence_facts`.
        """
        if not self._fitted:
            return Confidence.uncalibrated(f"{type(self).__name__}({self.target!r}) is not fitted")
        summary = self._fit_summary
        if self._from_seed:
            return Confidence(level=ConfidenceLevel.UNCALIBRATED, facts={
                "summary": f"{self.target!r} came from seed defaults, not from measured data",
                "from_seed": True, **self._confidence_facts(row)})
        if summary is None:
            # Distinct from the seed case: there *are* measured coefficients here, but the artifact
            # predates fit summaries (or was hand-written), so the support region is unrecoverable.
            # The level is still UNCALIBRATED because the right action is the same — recalibrate,
            # which refreshes the fit and records the summary — but the facts keep the two apart.
            return Confidence(level=ConfidenceLevel.UNCALIBRATED, facts={
                "summary": f"{self.target!r} has fitted parameters but no retained fit summary, "
                           f"so its support region is unknown",
                "has_fitted_params": True, "from_seed": False,
                **self._confidence_facts(row)})

        facts: dict = {"target": self.target, "n_points": summary.n_points,
                       "fitted_ranges": dict(summary.ranges),
                       "max_abs_residual": summary.max_abs_residual,
                       "max_rel_residual": summary.max_rel_residual}
        outside = summary.outside(row)

        if outside:
            # Leaving the sampled region outranks a confirmed form, and deliberately so.  An affine
            # law verified at every calibration point can still stop holding outside it — that is what
            # a regime boundary does (a burst-splitting limit in timing; a DSP-vs-LUT inference
            # threshold or a BRAM-vs-LUTRAM partitioning threshold in resources).  Zero residual is
            # evidence about the region that was measured, not a licence beyond it.
            #
            # But extrapolating a form that held at every point is a far better position than
            # extrapolating a noisy one, so that distinction rides in the facts where an agent can
            # weigh it, rather than being flattened away by the level.
            detail = ", ".join(f"{k}={v[0]:g} outside [{v[1]:g}, {v[2]:g}]"
                               for k, v in sorted(outside.items()))
            level = ConfidenceLevel.EXTRAPOLATED
            facts["outside"] = outside
            facts["form_exact"] = bool(summary.is_exact)
            facts["summary"] = f"{self.target}: {detail}"
            if summary.is_exact:
                facts["summary"] += (f"; the form did reproduce all {summary.n_points} fitted points "
                                     f"exactly, so the risk here is a regime change, not fit error")
        elif summary.is_exact:
            level = ConfidenceLevel.EXACT
            facts["summary"] = (f"{self.target}: form reproduces all {summary.n_points} calibration "
                                f"points exactly ({summary.n_free_params} free params)")
        else:
            level = ConfidenceLevel.INTERPOLATED
            facts["summary"] = (f"{self.target}: inside the calibrated region; worst residual on the "
                                f"corpus was {summary.max_rel_residual:.1%}")
            if summary.degenerate:
                facts["degenerate_fit"] = True
                facts["summary"] += (f" (fit had {summary.n_points} points for "
                                     f"{summary.n_free_params} free params — not over-determined)")

        facts.update(self._confidence_facts(row))
        return Confidence(level=level, facts=facts)

    def _confidence_facts(self, row) -> dict:
        """Extra model-specific facts merged into the confidence dict.  Override in a subclass.

        Free-form and JSON-able: the consumer is an agent reading the dict, so there is no shared
        vocabulary to conform to — say whatever this model form actually knows.
        """
        return {}

    def estimate(self, row, *, source: str = "") -> Estimate:
        """:meth:`predict` plus :meth:`confidence` — the reporting-time entry point.

        Deliberately separate from :meth:`predict`, which sits on the simulation hot path (called per
        firing, feeding straight into arithmetic) and must stay a bare float.  An estimate is built
        once per module when a report is assembled.
        """
        return Estimate(value=float(self.predict_feat(row)), source=source,
                        confidence=self.confidence_feat(row))

    def holdout_report(self, train, test) -> dict:
        """Fit on *train*, report R² and held-out residuals on *test* (frames/CalibDataFrames).

        Returns ``{"target", "r2_train", "test": [{pred, actual, rel_err}...], "max_rel_err"}``.
        """
        self.fit(train)
        recs = []
        for r in self._frame(test).to_dict("records"):
            actual = float(r[self.target])
            pred = self.predict_feat(r)
            rel = abs(pred - actual) / abs(actual) if actual else None
            recs.append({"pred": round(pred, 3), "actual": round(actual, 3),
                         "rel_err": round(rel, 4) if rel is not None else None})
        rels = [c["rel_err"] for c in recs if c["rel_err"] is not None]
        return {"target": self.target, "r2_train": round(self.score(train), 5),
                "test": recs, "max_rel_err": round(max(rels), 4) if rels else None}

    # -- parameters as a named dict (the state_dict) + artifact I/O ----------
    def to_params(self) -> dict:
        """This model's fitted parameters as a serializable **named dict** (the artifact payload —
        the analogue of a torch ``state_dict``).  Subclass-specific."""
        raise NotImplementedError

    def load_params(self, params: dict) -> "CalibModel":
        """Load *params* (from :meth:`to_params` or :attr:`seed`) into this model; returns ``self``
        (fitted).  The shell (basis / transform / names) is built in code — only the numbers come
        from *params*.  Subclass-specific."""
        raise NotImplementedError

    #: Reserved key under which the fit summary rides alongside a model's own ``to_params`` payload.
    #: Written and stripped generically here, so no subclass has to know about it.
    FIT_SUMMARY_KEY = "_fit_summary"

    def save_model(self, path=None) -> Path:
        """Write :meth:`to_params` as JSON to *path* (or :attr:`path`); returns the path.

        The :class:`~waveflow.calib.confidence.FitSummary` rides along under
        :attr:`FIT_SUMMARY_KEY` when one exists — that is what lets a *deployed* model (loaded in a
        project that never ran the sweep) still answer "you are extrapolating" rather than falling
        silent.  It is additive: an artifact written before this existed loads unchanged.
        """
        p = path if path is not None else self.path
        if p is None:
            raise ValueError(f"{type(self).__name__}({self.target!r}) has no path to save to")
        p = Path(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(self.to_params())
        if self._fit_summary is not None:
            payload[self.FIT_SUMMARY_KEY] = self._fit_summary.to_json()
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return p

    def load_model(self, path=None) -> "CalibModel | None":
        """Load params from *path* (or :attr:`path`) into this model and return ``self``; return
        ``None`` when the file is absent (so the caller can fall back to :meth:`default_model`)."""
        p = path if path is not None else self.path
        if p is None or not Path(p).exists():
            return None
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        summary = FitSummary.from_json(data.pop(self.FIT_SUMMARY_KEY, None)) if isinstance(data, dict) else None
        model = self.load_params(data)
        self._fit_summary = summary
        self._from_seed = False
        return model

    def default_model(self) -> "CalibModel":
        """Load the :attr:`seed` params (the default when there is no artifact / too little data)
        into this model; returns ``self``.

        Marks the model as seed-backed, so :meth:`confidence` reports ``UNCALIBRATED`` rather than
        letting a default masquerade as a measurement — today this fallback is entirely silent.
        """
        if self.seed is None:
            raise RuntimeError(f"{type(self).__name__}({self.target!r}) has no seed")
        model = self.load_params(self.seed)
        self._from_seed = True
        self._fit_summary = None
        return model

    def load_or_default(self, path=None) -> "CalibModel":
        """:meth:`load_model` if the artifact exists, else :meth:`default_model` (the seed)."""
        return self.load_model(path) or self.default_model()


# ---------------------------------------------------------------------------
# LinCalibModel — sklearn LinearRegression over the basis columns
# ---------------------------------------------------------------------------

@dataclass
class LinCalibModel(CalibModel):
    """Linear least-squares model over the :attr:`basis` columns, backed by
    ``sklearn.LinearRegression``.

    *fit_intercept* adds a constant term (sklearn fits it separately); pass
    ``fit_intercept=False`` for a through-origin model whose coefficients are the
    physical per-feature rates (e.g. FIR's ``span = setup·num_trans + per_word·nwords``).

    *transform* lets the **model own its basis map**: a callable ``row -> [feature, ...]`` used by
    BOTH :meth:`design` (fit) and :meth:`_predict_one` (predict), so the raw-inputs → basis-functions
    mapping lives in ONE place and cannot drift between fit and deploy.  With it, :attr:`basis`
    merely *names* the produced features (for :attr:`coeffs` / :meth:`as_dict`); without it the
    features are the ``df[basis]`` columns (the caller-side derived-column idiom).  Fold any input
    into the features here — e.g. the FIR times carry ``clk_period`` in the row so the fit is
    **clock-independent** (``fill_time = clk_period·L0``; ``compute_time = clk_period·[(nr-1)·row_setup
    + nr·nc·beat]``) and a clock change needs no re-fit.

    A fitted model caches its coefficients (sklearn is used only in :meth:`fit`); a **deployed**
    model built via :meth:`load_params` (the base's :meth:`load_or_default`) predicts from those
    coefficients alone — no sklearn, no training data.

    *coeff_names* / *intercept_name* shape the :meth:`to_params` state_dict (the artifact): with
    ``coeff_names`` the coeffs are stored **individually** (``{"L0": 60.0}``); with ``None`` they
    are a flat vector (``{"coeffs": [c0, c1, ...]}``); the bias is stored under ``intercept_name``
    (only when ``fit_intercept``).  ``basis`` selects the design-matrix columns (when there's no
    *transform*); the coefficient *names* come from ``coeff_names`` (falling back to ``basis``).
    """

    fit_intercept: bool = True
    #: ``callable(params) -> {feature: value}`` -- an inline :meth:`~CalibModel.transform`, for a
    #: derived basis supplied at construction rather than by subclassing.  Same contract as the
    #: method: parameters in, features out, **no component**.  (Was ``basis_fn`` and returned a bare
    #: vector; it now returns a named mapping so a feature and its coefficient share a name.)
    transform_fn: "Callable[[dict], dict] | None" = None
    coeff_names: "list[str] | None" = None
    intercept_name: str = "intercept"

    def __post_init__(self) -> None:
        super().__post_init__()
        self._coef: "np.ndarray | None" = None
        self._intercept: float = 0.0

    def _param_names(self) -> list[str]:
        """Coefficient names for introspection / individual serialization (``coeff_names`` or
        the design ``basis``)."""
        return list(self.coeff_names) if self.coeff_names is not None else list(self.basis)

    # -- feature map (the model owns its basis transform) -------------------
    def _feature_names(self) -> list:
        """Names of the design-vector entries, in order.

        :attr:`basis` when there is one — it names the *features*.  ``coeff_names`` is a display
        alias for the fitted coefficients and is only load-bearing here when a model has no basis at
        all, which is the shape a pure :attr:`transform_fn` model takes (``basis=[]``).
        """
        return list(self.basis) if self.basis else list(self.coeff_names or [])

    def _features(self, row) -> np.ndarray:
        """Parameter *row* -> the numeric design vector, via :meth:`transform`."""
        feats = self.transform(row)
        return np.asarray([float(feats[n]) for n in self._feature_names()], dtype=float)

    def transform(self, params: dict) -> dict:
        """:attr:`transform_fn` when given, else the base identity.

        The inline form of overriding :meth:`~CalibModel.transform` — for a derived basis built at
        construction rather than by subclassing.
        """
        if self.transform_fn is not None:
            return dict(self.transform_fn(params))
        return super().transform(params)

    def design(self, df: pd.DataFrame) -> np.ndarray:
        if len(df) == 0:
            return np.empty((0, len(self.basis)), dtype=float)
        return np.array([self._features(r) for r in df.to_dict("records")], dtype=float)

    # -- fit / predict ------------------------------------------------------
    def fit(self, data=None) -> "LinCalibModel":
        from sklearn.linear_model import LinearRegression

        df = self._frame(self._fit_data(data))
        reg = LinearRegression(fit_intercept=self.fit_intercept).fit(
            self.design(df), self.target_column(df))
        self._coef = np.asarray(reg.coef_, dtype=float)
        self._intercept = float(reg.intercept_) if self.fit_intercept else 0.0
        self._fitted = True
        self._record_fit_summary(df)
        return self

    def n_free_params(self) -> int:
        """One coefficient per design column, plus the intercept when fitted."""
        n = len(self._coef) if self._fitted else len(self.basis)
        return int(n) + (1 if self.fit_intercept else 0)

    def _predict_one(self, row) -> float:
        return self._intercept + float(np.asarray(self._coef, dtype=float) @ self._features(row))

    # -- state_dict (the artifact payload) ---------------------------------
    def to_params(self) -> dict:
        """State_dict: coeffs named individually when :attr:`coeff_names` is set, else a flat
        ``{"coeffs": [...]}`` vector; the intercept under :attr:`intercept_name` if
        ``fit_intercept``."""
        if not self._fitted:
            raise RuntimeError("not fitted")
        if self.coeff_names is not None:
            d = {n: float(c) for n, c in zip(self.coeff_names, self._coef)}
        else:
            d = {"coeffs": [float(c) for c in self._coef]}
        if self.fit_intercept:
            d[self.intercept_name] = float(self._intercept)
        return d

    def load_params(self, params: dict) -> "LinCalibModel":
        """Load coeffs (named by :attr:`coeff_names`, else the ``"coeffs"`` vector) + optional
        intercept from a :meth:`to_params` / seed dict — the deploy path (no sklearn / data)."""
        if self.coeff_names is not None:
            self._coef = np.asarray([float(params[n]) for n in self.coeff_names], dtype=float)
        else:
            self._coef = np.asarray(params["coeffs"], dtype=float)
        self._intercept = float(params.get(self.intercept_name, 0.0)) if self.fit_intercept else 0.0
        self._fitted = True
        return self

    # -- introspection ------------------------------------------------------
    @property
    def coeffs(self) -> dict:
        """Fitted ``{name: coefficient}`` (named by ``coeff_names`` or ``basis``), plus the
        intercept under :attr:`intercept_name` if ``fit_intercept``."""
        if not self._fitted:
            raise RuntimeError("not fitted")
        d = {n: float(c) for n, c in zip(self._param_names(), self._coef)}
        if self.fit_intercept:
            d[self.intercept_name] = float(self._intercept)
        return d

    def as_dict(self) -> dict:
        """Rich descriptor ``{target, basis, coeffs, fit_intercept}`` (for inspection; the
        deployable artifact is :meth:`to_params`)."""
        return {"target": self.target, "basis": list(self.basis),
                "coeffs": self.coeffs, "fit_intercept": self.fit_intercept}

    def plot(self, data, x_name: str, ax=None, label: str | None = None):
        """Scatter actual vs. fitted against the *x_name* column (simple diagnostic).

        Returns the matplotlib ``Axes``.  Import is local so the package has no hard
        matplotlib dependency."""
        import matplotlib.pyplot as plt

        df = self._frame(data)
        xs = df[x_name].to_numpy(dtype=float)
        order = np.argsort(xs)
        actual = self.target_column(df)
        pred = np.array([self.predict_feat(r) for r in df.to_dict("records")], dtype=float)
        if ax is None:
            _, ax = plt.subplots()
        tag = label or self.target
        ax.scatter(xs, actual, label=f"{tag} actual", marker="o")
        ax.plot(xs[order], pred[order], label=f"{tag} fit", linestyle="--")
        ax.set_xlabel(x_name)
        ax.set_ylabel(self.target)
        ax.legend()
        return ax


# ---------------------------------------------------------------------------
# InterpCalibModel — calibrated 1-D lookup (a measured table, not a curve fit)
# ---------------------------------------------------------------------------

@dataclass
class InterpCalibModel(CalibModel):
    """Piecewise-linear interpolation over a **single** basis column — a calibrated
    *lookup*, not a curve fit.

    For a physical quantity that is genuinely non-linear but smooth and **saturating** (e.g. a
    per-row pipeline / ping-pong depth as a function of row length): sample it densely enough
    that linear interpolation between samples is clean, and **clamp (flat-extrapolate) beyond
    the sampled range** — exactly the saturation behaviour.  This is the principled alternative
    to forcing a wrong basis (a ``sqrt`` fudge) onto a measured curve: carry the measurement.

    Duplicate feature values are averaged (e.g. ``row_depth(n_col)`` measured at several ``n_row``).
    """

    def __post_init__(self) -> None:
        super().__post_init__()
        if len(self.basis) != 1:
            raise ValueError("InterpCalibModel supports exactly one feature")
        self._xs = None
        self._ys = None

    def fit(self, data=None) -> "InterpCalibModel":
        df = self._frame(self._fit_data(data))
        f = self.basis[0]
        xy: dict[float, list[float]] = {}
        for x, y in zip(df[f].to_numpy(dtype=float), df[self.target].to_numpy(dtype=float)):
            xy.setdefault(float(x), []).append(float(y))
        keys = sorted(xy)
        self._xs = np.array(keys, dtype=float)
        self._ys = np.array([float(np.mean(xy[k])) for k in keys], dtype=float)
        self._fitted = True
        self._record_fit_summary(df)
        return self

    def n_free_params(self) -> int:
        """One free value per table knot.

        So a lookup never claims ``EXACT``: it reproduces its knots because it *is* its knots, which is
        the degenerate case an exactness claim must exclude.  Only a fit with repeated measurements at
        a knot (which averaging must then reconcile) is over-determined at all.
        """
        return int(len(self._xs)) if self._fitted and self._xs is not None else len(self.basis)

    def _confidence_facts(self, row) -> dict:
        """Note the deliberate clamp, so an out-of-range query is not read as a plain failure.

        Flat extrapolation past the sampled range *is* this model's physical claim (a saturating
        quantity), not an accident — but it is still a claim about unmeasured territory, so the level
        stays ``EXTRAPOLATED`` and this explains what the number actually is.
        """
        if not self._fitted or self._xs is None or len(self._xs) == 0:
            return {}
        f = self.basis[0]
        if f not in row:
            return {}
        try:
            x = float(row[f])
        except (TypeError, ValueError):
            return {}
        lo, hi = float(self._xs[0]), float(self._xs[-1])
        if x < lo or x > hi:
            return {"clamped": True, "clamped_to": lo if x < lo else hi,
                    "model_form": "piecewise-linear lookup, flat (saturating) beyond the sampled range"}
        return {"model_form": "piecewise-linear lookup", "knots": int(len(self._xs))}

    def _predict_one(self, row) -> float:
        # np.interp clamps to the endpoint values outside [xs[0], xs[-1]] — the saturation.
        return float(np.interp(float(row[self.basis[0]]), self._xs, self._ys))

    @property
    def samples(self) -> dict:
        """The calibrated table ``{"feature", "x", "y"}`` (serializable)."""
        if not self._fitted:
            raise RuntimeError("not fitted")
        return {"feature": self.basis[0],
                "x": [float(v) for v in self._xs], "y": [float(v) for v in self._ys]}

    # -- state_dict (the artifact payload) = the measured (x, y) table ------
    def to_params(self) -> dict:
        return self.samples

    def load_params(self, params: dict) -> "InterpCalibModel":
        """Load the measured ``(x, y)`` table from a :meth:`to_params` / seed dict (no re-fit)."""
        self._xs = np.asarray(params["x"], dtype=float)
        self._ys = np.asarray(params["y"], dtype=float)
        self._fitted = True
        return self

    @classmethod
    def from_samples(cls, feature: str, xs, ys, target: str = "") -> "InterpCalibModel":
        """Build directly from a stored ``(xs, ys)`` table (the deserialize path)."""
        return cls([feature], target).load_params({"x": xs, "y": ys})


# ---------------------------------------------------------------------------
# Lookup — memorize, and refuse to interpolate
# ---------------------------------------------------------------------------

@dataclass
class LookupCalibModel(CalibModel):
    """Return the measurement for exactly this feature point; refuse to interpolate.

    A **memorizing** model: its parameters are the table itself, one row per measured point, so it
    fits like any other model and stores like any other model.  What it will not do is answer between
    the points -- an unmeasured feature point gets ``UNCALIBRATED``, not the nearest entry.

    That refusal is the design rather than a limitation.  A lookup makes **no assumption** about the
    shape of the function, so nothing it assumes can be wrong; the price is that it answers only where
    you measured.  It is the right kind wherever the underlying law has binding thresholds -- a
    multiply that stops fitting one DSP, an array that stops fitting block RAM, a loop that stops
    pipelining at II=1 -- because between two measured points the truth can be anywhere, and a model
    that interpolated would be confidently wrong exactly where it matters.

    Axis-agnostic by construction: it keys on the :attr:`basis` columns and stores the
    :attr:`targets` columns, neither of which knows whether a row came from an RTL run or a synthesis
    report.  That is the claim the harmonization rests on -- see
    ``tests/calib/test_harmonized_base.py``.
    """

    #: ``{feature tuple: {target: value}}`` -- the fitted parameters.  Populated by :meth:`fit`, or
    #: supplied directly for a committed table.
    table: dict = field(default_factory=dict)

    @property
    def has_free_params(self) -> bool:
        return True                     # the table IS the parameters

    def n_free_params(self) -> int:
        """One free value per stored cell — which is why a lookup never claims to generalize."""
        return sum(len(v) for v in self.table.values())

    # -- keying -------------------------------------------------------------
    def _key(self, feats) -> tuple:
        """The table key for a feature mapping: its :attr:`basis` values, in order.

        Values are normalized through ``float`` where possible so that ``4`` and ``4.0`` -- the same
        point written by a CSV round-trip and by a live component -- do not become two entries, one
        of which is then never found.
        """
        out = []
        for b in self.basis:
            if b not in feats:
                raise KeyError(
                    f"{type(self).__name__}({self.name!r}): feature {b!r} missing from {sorted(feats)}")
            v = feats[b]
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                out.append(str(v))
        return tuple(out)

    # -- fit / predict ------------------------------------------------------
    def fit(self, data=None) -> "LookupCalibModel":
        """Record one table row per measured point, from the corpus by default.

        A repeated feature point **overwrites**, so a re-measurement supersedes an older one without
        the caller pruning the corpus -- the same rule
        :meth:`~waveflow.calib.record_store.ModuleStore.best` applies on the record side.
        """
        df = self._frame(self._fit_data(data))
        for row in df.to_dict("records"):
            cell = {t: row[t] for t in self.targets if t in row and pd.notna(row[t])}
            if cell:
                self.table[self._key(row)] = cell
        self._fitted = bool(self.table)
        self._record_fit_summary(df)
        return self

    def predict_feat(self, feats):
        """The stored measurement, or zero for an unmeasured point.

        Zero rather than a raised error because a composed estimate must still produce a number --
        but it is always paired with an ``UNCALIBRATED`` :meth:`confidence_feat` naming the gap, so a
        missing measurement can never read as a genuine zero.  See ``docs/guide/calib/confidence.md``.
        """
        cell = self.table.get(self._key(feats), {})
        if len(self.targets) == 1:
            return float(cell.get(self.targets[0], 0.0))
        return {t: cell.get(t, 0) for t in self.targets}

    def confidence_feat(self, feats) -> Confidence:
        key = self._key(feats)
        cell = self.table.get(key)
        if not cell:
            return Confidence(level=ConfidenceLevel.UNCALIBRATED, facts={
                "summary": f"{self.name}: no measurement stored at {dict(zip(self.basis, key))}; a "
                           f"lookup cannot interpolate, so this is a gap, not an estimate",
                "model": "lookup", "basis": list(self.basis),
                "point": dict(zip(self.basis, key)), "n_stored": len(self.table)})
        return Confidence(level=ConfidenceLevel.EXACT, facts={
            "summary": f"{self.name}: measured directly at this point",
            "model": "lookup", "basis": list(self.basis),
            "point": dict(zip(self.basis, key)), "n_stored": len(self.table)})

    # -- state_dict ---------------------------------------------------------
    def to_params(self) -> dict:
        """The table, with tuple keys flattened to strings so it round-trips through JSON."""
        return {"basis": list(self.basis), "targets": list(self.targets),
                "table": [{"point": list(k), "values": v} for k, v in self.table.items()]}

    def load_params(self, params: dict) -> "LookupCalibModel":
        self.table = {tuple(e["point"]): dict(e["values"]) for e in params.get("table", [])}
        self._fitted = bool(self.table)
        return self


# ---------------------------------------------------------------------------
# Prior — a formula, nothing fitted
# ---------------------------------------------------------------------------

@dataclass
class PriorCalibModel(CalibModel):
    """Targets computed from a **formula** over the features.  No free parameters.

    For quantities that follow a known rule rather than needing estimation -- a multiply too wide for
    one DSP takes two, an array too large for LUTRAM lands in block RAM, a loop at II=1 retires one
    beat per cycle.  Those are *decisions* the tool makes by a rule, so they are encoded and checked
    rather than learned.

    A prior that reproduces every measured point with **zero** free parameters is a stronger claim
    than a regression that fits them, and needs no held-out validation to be believed.  The claim is
    falsifiable, and :meth:`fit` is what falsifies it.

    Axis-agnostic: a formula over features does not care whether they came from an RTL run or a
    synthesis report.
    """

    #: ``{target: callable(features) -> value}``.  Targets absent here are simply not predicted --
    #: never defaulted to zero, which would make a design read as cheaper than it is.
    formulas: dict = field(default_factory=dict)
    #: Optional ``callable(params) -> dict`` overriding :meth:`transform` -- the derivation, inline.
    transform_fn: "Callable[[dict], dict] | None" = None

    @property
    def targets(self) -> "tuple[str, ...]":
        return tuple(self.formulas)

    @property
    def has_free_params(self) -> bool:
        return False

    def n_free_params(self) -> int:
        return 0

    def transform(self, params: dict) -> dict:
        if self.transform_fn is not None:
            return dict(self.transform_fn(params))
        return super().transform(params)

    def predict_feat(self, row):
        feats = self.transform(row)
        out = {t: fn(feats) for t, fn in self.formulas.items()}
        return out[self.targets[0]] if len(self.targets) == 1 else out

    def confidence_feat(self, row) -> Confidence:
        """``EXACT`` -- the claim being made is *this is the rule*, not *this was fitted*.

        A zero-parameter formula either reproduces the measurements or it does not, and :meth:`fit`
        records which in the fit summary.  A prior that turns out to be wrong is a **bug in the
        rule**, which is a different thing from an uncalibrated model; reporting it as
        ``UNCALIBRATED`` would hide it among the models that merely lack data.
        """
        return Confidence(level=ConfidenceLevel.EXACT, facts={
            "summary": f"{self.name}: {', '.join(sorted(self.formulas))} from an analytical prior "
                       f"with no fitted parameters",
            "model": "prior", "targets": sorted(self.formulas),
            "inputs": self.transform(row)})

    def fit(self, data=None) -> "PriorCalibModel":
        """Nothing to fit -- but the corpus is still **checked** against the formula.

        A prior claims to reproduce measurement exactly.  Recording the residual here is what turns
        that into something the fit summary can contradict; no coefficient moves, because none exist.
        """
        self._record_fit_summary(self._frame(self._fit_data(data)))
        return self

    def to_params(self) -> dict:
        return {"model": "prior", "targets": list(self.targets)}

    def load_params(self, params: dict) -> "PriorCalibModel":
        return self          # a formula carries no numbers to load


# ---------------------------------------------------------------------------
# Concat — one model per target, presented as one
# ---------------------------------------------------------------------------

@dataclass
class ConcatCalibModel(CalibModel):
    """Several models composed **across targets** -- each answers for the targets it covers.

    The common real shape: DSP and block-RAM counts follow device rules exactly, while LUT and FF
    have no closed form and must be regressed.  Forcing one kind to answer for both means either
    fitting something already known, or asserting a formula where none exists.  This composes them,
    so **each target comes from whichever model is honest for it**.

    Distinct from :func:`~waveflow.calib.resource_model.compose`, which sums across the *modules* of
    a hierarchy.  This covers the targets of a *single* model and sums nothing: the sub-models
    partition the targets rather than contributing to them.
    """

    #: Sub-models in precedence order -- an earlier model wins a target a later one also claims.
    #: A sequence rather than a mapping so the precedence is visible at the construction site.
    models: tuple = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        self.models = tuple(self.models)

    def sub_models(self) -> tuple:
        """The composed models.  :attr:`models` by default.

        A method rather than a bare attribute read so a subclass can *assemble* its sub-models from
        its own state -- one built from device rules plus one regression per fitted target, say --
        without having to keep a parallel tuple in sync with the state it was derived from.
        """
        return self.models

    def _owner_by_target(self) -> dict:
        """``{target: model}`` -- the first model claiming a target owns it."""
        out: dict = {}
        for m in self.sub_models():
            for t in m.targets:
                out.setdefault(t, m)
        return out

    @property
    def targets(self) -> "tuple[str, ...]":
        return tuple(self._owner_by_target())

    @property
    def has_free_params(self) -> bool:
        return any(m.has_free_params for m in self.sub_models())

    def n_free_params(self) -> int:
        return sum(m.n_free_params() for m in self.sub_models() if m.has_free_params)

    def get_params(self, comp, **runtime) -> dict:
        """The **union** of what every sub-model needs, so one corpus row serves all of them.

        A row satisfying only some sub-models would make the concat unfittable from its own corpus --
        the failure the params/transform split exists to prevent, reintroduced one level up.
        """
        params: dict = {}
        for m in self.sub_models():
            params.update(m.get_params(comp, **runtime))
        return params

    def predict_feat(self, row):
        """Each target from its owning model.  Always a mapping -- a concat exists to have several."""
        out: dict = {}
        for t, m in self._owner_by_target().items():
            got = m.predict_feat(row)
            out[t] = got[t] if isinstance(got, dict) else got
        return out

    def confidence_feat(self, row) -> Confidence:
        """The **weakest** sub-confidence, naming which target sits there.

        Not an average.  An estimate is believable only to the extent of its least believable part,
        and a concat reporting ``EXACT`` because two of its four targets came from device rules would
        be the most misleading thing this layer could do.
        """
        per = {t: m.confidence_feat(row) for t, m in self._owner_by_target().items()}
        if not per:
            return Confidence.uncalibrated(f"{self.name}: no sub-models")
        worst = min(per.values(), key=lambda c: c.level)     # ConfidenceLevel orders by rank
        weakest = sorted(t for t, c in per.items() if c.level is worst.level)
        return Confidence(level=worst.level, facts={
            "summary": f"{self.name}: {worst.level.value}; weakest target(s) {', '.join(weakest)}",
            "model": "concat", "weakest": weakest,
            "per_target": {t: c.to_json() for t, c in per.items()}})

    def fit(self, data=None) -> "ConcatCalibModel":
        """Fit every sub-model on the same corpus.  One with nothing to fit is a no-op."""
        df = self._frame(self._fit_data(data))
        for m in self.sub_models():
            m.fit(df)
        self._fitted = True
        self._record_fit_summary(df)
        return self

    def to_params(self) -> dict:
        return {"model": "concat",
                "sub": [{"name": m.name, "targets": list(m.targets), "params": m.to_params()}
                        for m in self.sub_models()]}

    def load_params(self, params: dict) -> "ConcatCalibModel":
        by_name = {m.name: m for m in self.sub_models()}
        for blob in params.get("sub", []):
            m = by_name.get(blob.get("name"))
            if m is not None:
                m.load_params(blob.get("params") or {})
        self._fitted = True
        return self
