"""timing_model.py — per-component timing calibration, orchestration over :mod:`calib`.

:mod:`waveflow.calib.calib` has the *model* machinery (fit / predict / state_dict / seed) but, as its
own docstring says, "no infrastructure for collecting data".  :class:`TimingModel` is that missing
layer: it collects a component's per-firing measurements from an RTL trace and from pysim, fits the
**residual** (the delay pysim is missing) with a composed :class:`~waveflow.calib.calib.CalibModel`,
and exposes a ``predict`` the component's ``run_iter`` calls.

See ``plans/timing_model.md``.  The design in four pieces, each earned the hard way:

* **The target is the residual, corrected for the current prediction:**
  ``residual = rtl_span - pysim_span + current_dly``.  The pysim span already includes whatever the
  model predicted on that run, so without ``+ current_dly`` the fit would double-count.  With it the
  fit is self-correcting — any run is a datapoint (no model-disabled baseline), and online
  calibration converges from a 0-seed.
* **Validity is per-firing and overridable** (:meth:`is_record_valid`, default ``blocked == 0``).  A
  firing that stalled on a full downstream channel measured contention, not the component's own cost.
  A subclass can inspect the run folder (the AXI trace) for a richer stall test.
* **RTL and pysim are collected independently** into ``rtl/`` and ``pysim/`` trees — different
  cadence (RTL is Vitis-expensive, pysim is every-edit-cheap).  They are joined at fit time on the
  **feature point** (``nwords``, ``num_trans``), not the run id.  Feature points present on only one
  side cannot yield a residual and are reported, not silently dropped.
* **Everything internal is in CYCLES**, so ``params.json`` is clock-independent (reused unchanged
  across a re-synth at a different clock).  Conversion is only at the boundary: RTL is natively
  cycles; pysim is time, so :meth:`collect_pysim` divides by ``clk.period`` and :meth:`predict`
  multiplies back.  With ``clk=None`` the data is assumed already in cycles (tests, pure analysis).
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from waveflow.calib.calib import LinCalibModel

#: Fit-target column name (the residual, in cycles).
RESIDUAL = "residual"


@dataclass
class TimingModel:
    """A component's timing residual model plus the corpus that fits it.

    Parameters
    ----------
    component : str
        The identifier this component's firings carry in the timing table — the task-body ``id``
        (``"mem_w_stream_framed_done_task"``) or the RTL ``inst``.  :meth:`collect_rtl` filters by it.
    calib_dir : str | Path
        Corpus + artifact directory: independent ``rtl/<run>/firings.csv`` and
        ``pysim/<run>/firings.csv`` trees, the fitted ``params.json``, and a derived ``corpus.csv``
        (the merged frame, a cache).
    features : list[str]
        The regression basis, columns present on *both* sides (``["nwords", "num_trans"]``).
    num_targets : int
        Delays :meth:`predict` returns (default 1).  ``> 1`` is the API for a delay *between*
        internal stages, but needs per-stage measurement (Tier-2 tracing) — every op raises for now.
    placement : str
        ``"leading"`` | ``"trailing"`` — advisory: where ``run_iter`` injects the delay.
    seed : dict | None
        Seed params (the ``state_dict``) used by :meth:`predict` before any fit, so a fresh component
        still simulates.  ``None`` seeds to zero additional delay.
    clk : Any
        Anything with a ``.period``.  When set, :meth:`collect_pysim` converts its time spans to
        cycles and :meth:`predict` returns time.  ``None`` = data already in cycles.
    """

    component: str
    calib_dir: str | Path
    features: list[str] | None = None
    num_targets: int = 1
    placement: str = "trailing"
    seed: dict | None = None
    clk: Any = None
    #: Whether this residual is keyed to the component's **configuration** (its template arguments) or
    #: only to its function name.  A library written before that distinction existed holds bare-named
    #: directories, and a residual fit at one memory width would otherwise be served silently to a
    #: design at another; ``False`` surfaces that in :meth:`confidence` instead of implying a match.
    config_specific: bool = True
    _model: LinCalibModel = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.features:
            raise ValueError(f"{type(self).__name__} requires `features` (the regression basis)")
        self.calib_dir = Path(self.calib_dir)
        self.features = [str(f) for f in self.features]
        #: Populated by :meth:`gen_data_frame`: {"matched", "rtl_only", "pysim_only"} feature points.
        self.coverage: dict = {}
        seed = self.seed if self.seed is not None else {
            **{f: 0.0 for f in self.features}, "intercept": 0.0}
        self._model = LinCalibModel(
            basis=self.features, target=RESIDUAL, fit_intercept=True,
            coeff_names=self.features, seed=seed, path=self.calib_dir / "params.json")

    # -- paths & unit conversion -------------------------------------------
    @property
    def corpus_path(self) -> Path:
        return self.calib_dir / "corpus.csv"

    def _run_dir(self, side: str, run_id: str) -> Path:
        d = self.calib_dir / side / str(run_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _to_cycles(self, t: float) -> float:
        return float(t) if self.clk is None else float(t) / self.clk.period

    def _to_time(self, c: float) -> float:
        return float(c) if self.clk is None else float(c) * self.clk.period

    def _guard_single(self) -> None:
        if self.num_targets != 1:
            raise NotImplementedError(
                f"num_targets={self.num_targets}: multi-target calibration needs per-stage "
                f"measurement (Tier-2 intra-component tracing). The API is carried; the "
                f"implementation is single-target for now.")

    # -- validity (per firing; overridable) --------------------------------
    def is_record_valid(self, firing: dict, run_dir: Path) -> bool:
        """Is this **RTL** firing a clean measurement of the component's own cost?

        Applied only to the RTL side (a pysim firing is the model being calibrated, not a
        measurement to validate).  Default: ``blocked == 0`` — the column :class:`ExtractBurstsStep`
        computes from FIFO occupancy.  Override to inspect *run_dir* (e.g. scan an ``m_axi`` bundle's
        ``beat_type`` for stalls the FIFO counters do not see); an override may freely assume the RTL
        firing columns (``index``, ``blocked``, …).
        """
        return int(firing.get("blocked", 0) or 0) == 0

    # -- collection (independent rtl / pysim trees) ------------------------
    def collect_rtl(self, events: dict, run_id: str) -> Path:
        """Write this component's firings from an ``ExtractBurstsStep`` *events* dict into
        ``rtl/<run_id>/firings.csv`` (spans already in cycles).  Keeps ALL firings — the fit filters
        via :meth:`is_record_valid`; the full set is the debug trail."""
        self._guard_single()
        rows = [f for f in events.get("firings", []) if f["component"] == self.component]
        out = self._run_dir("rtl", run_id) / "firings.csv"
        pd.DataFrame(rows).to_csv(out, index=False)
        return out

    def collect_pysim(self, firings: list[dict], run_id: str) -> Path:
        """Write this component's pysim firings into ``pysim/<run_id>/firings.csv``.

        Each firing carries the *features*, ``span``, and ``current_dly`` (what :meth:`predict`
        returned on this run).  ``span``/``current_dly`` arrive in **time** and are divided by
        ``clk.period`` to cycles here (unless ``clk is None``), so the stored corpus is all-cycles."""
        self._guard_single()
        conv = []
        for f in firings:
            g = {**f, "span": self._to_cycles(f["span"])}
            if "current_dly" in f:
                g["current_dly"] = self._to_cycles(f["current_dly"])
            conv.append(g)
        df = pd.DataFrame(conv)
        missing = ({"span", "current_dly", *self.features}) - set(df.columns)
        if firings and missing:
            raise ValueError(f"pysim firings missing {sorted(missing)}; got {list(df.columns)}")
        out = self._run_dir("pysim", run_id) / "firings.csv"
        df.to_csv(out, index=False)
        return out

    # -- parsing: one run -> its aggregated point --------------------------
    def get_params(self, run_dir: Path, validate: bool = True) -> dict | None:
        """Parse one run folder to a single ``{feature: value, measurement: median}`` row, or
        ``None`` if it holds no valid firing.

        When *validate* (the RTL side), firings are filtered through :meth:`is_record_valid` first;
        the pysim side passes ``validate=False`` (its firings are the model, not a measurement).
        Survivors are aggregated (median span over the run's firings).  Features are constant within
        a run (one run = one scenario), so they pass through as-is.  This is the per-model "how to
        read a run" hook — a subclass whose run folder holds extra artifacts can override it."""
        p = Path(run_dir) / "firings.csv"
        if not p.exists() or p.stat().st_size == 0:
            return None
        df = pd.read_csv(p)
        if df.empty:
            return None
        if validate:
            df = df[[self.is_record_valid(r, Path(run_dir)) for r in df.to_dict("records")]]
        valid = df
        if len(valid) == 0:
            return None
        row = {f: float(valid[f].iloc[0]) for f in self.features}
        for c in valid.columns:
            if c not in self.features and pd.api.types.is_numeric_dtype(valid[c]):
                row[c] = float(valid[c].median())
        return row

    def _side_frame(self, side: str, value_cols: list[str]) -> pd.DataFrame:
        """Every run under ``<side>/`` reduced to one row per feature point (median across runs at
        the same point), keeping only *features* + *value_cols*."""
        rows = []
        base = self.calib_dir / side
        for run_dir in sorted(p for p in base.glob("*") if p.is_dir()):
            r = self.get_params(run_dir, validate=(side == "rtl"))
            if r and all(c in r for c in value_cols):
                rows.append({**{f: r[f] for f in self.features}, **{c: r[c] for c in value_cols}})
        if not rows:
            return pd.DataFrame(columns=[*self.features, *value_cols])
        return pd.DataFrame(rows).groupby(self.features, as_index=False)[value_cols].median()

    # -- assemble: the merged residual frame -------------------------------
    def gen_data_frame(self) -> pd.DataFrame:
        """Join the RTL and pysim corpora on the feature point into one inspectable, fittable frame:

        ``features… | span_rtl | span_pysim | current_dly | residual``

        Records coverage on :attr:`coverage` — which feature points matched, and which appear on
        only one side (they cannot yield a residual, so a thin fit is a *visible* coverage gap, not a
        silent one).  Caches the frame to ``corpus.csv``."""
        self._guard_single()
        rtl = self._side_frame("rtl", ["span"])
        py = self._side_frame("pysim", ["span", "current_dly"])

        def keys(df):
            return {tuple(v) for v in df[self.features].to_numpy().tolist()}

        kr, kp = keys(rtl), keys(py)
        self.coverage = {"matched": sorted(kr & kp),
                         "rtl_only": sorted(kr - kp), "pysim_only": sorted(kp - kr)}

        cols = [*self.features, "span_rtl", "span_pysim", "current_dly", RESIDUAL]
        if rtl.empty or py.empty:
            return pd.DataFrame(columns=cols)

        merged = rtl.merge(py, on=self.features, how="inner", suffixes=("_rtl", "_pysim"))
        merged[RESIDUAL] = merged["span_rtl"] - merged["span_pysim"] + merged["current_dly"]
        out = merged[cols]
        self.corpus_path.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(self.corpus_path, index=False)
        return out

    # -- fit / deploy ------------------------------------------------------
    def fit(self) -> "TimingModel":
        """Build the merged frame and fit the residual model; write ``params.json``.  Raises if
        nothing joined, so a silent no-op cannot leave a seed model looking fitted."""
        df = self.gen_data_frame()
        if len(df) == 0:
            raise RuntimeError(
                f"{self.component}: no residual datapoints — RTL and pysim did not join on "
                f"{self.features}. Coverage: {self.coverage}")
        self._model.fit(df)
        self._model.save_model()
        return self

    def predict(self, row: dict) -> list[float]:
        """Additional delay(s), in **time** (``clk`` set) or cycles (``clk=None``), for a firing
        described by *row*.  Length :attr:`num_targets`.  An unfitted model falls back to the seed,
        so this is safe before any calibration.  Clamped at 0 — a negative timeout is meaningless."""
        self._guard_single()
        if not self._model._fitted:
            self._model.load_or_default()
        return [self._to_time(max(0.0, self._model.predict(row)))]

    def confidence(self, row: dict):
        """How much :meth:`predict` should be believed for *row*
        (:class:`~waveflow.calib.confidence.Confidence`).

        The passthrough a per-module report needs: without it nothing outside this class can reach the
        underlying model's confidence, and a report would have to choose between showing a number with
        no provenance or showing nothing.  Loads lazily on the same terms as :meth:`predict`, so
        asking about confidence never changes what a prediction would return.
        """
        self._guard_single()
        if not self._model._fitted:
            self._model.load_or_default()
        conf = self._model.confidence(row)
        conf.facts.setdefault("component", self.component)
        conf.facts.setdefault("config_specific", bool(self.config_specific))
        if not self.config_specific:
            # Found only under the bare function name, so it is not known to describe *this*
            # configuration — a residual fit at one memory width would otherwise be reported with the
            # same confidence as one fit at this width.
            conf.facts["summary"] = (
                f"{conf.facts.get('summary', '')}; residual is keyed by function name only "
                f"({self.component}), not by configuration, so it may have been fit at a different "
                f"width").lstrip("; ")
        return conf

    def estimate(self, row: dict, *, source: str = "pysim"):
        """:meth:`predict` paired with :meth:`confidence` — one entry for assembling a report."""
        from waveflow.calib.confidence import Estimate

        return Estimate(value=float(self.predict(row)[0]), source=source,
                        confidence=self.confidence(row))

    # -- lifecycle ---------------------------------------------------------
    def reset(self, corpus: bool = True, params: bool = False) -> None:
        """Wipe the corpus and/or the fitted params, to recalibrate from scratch.

        ``corpus=True`` (default) clears the ``rtl/`` and ``pysim/`` trees and ``corpus.csv`` — the
        common "re-sweep from scratch".  ``params=True`` deletes ``params.json`` so the next
        :meth:`predict` falls back to the seed."""
        if corpus:
            for side in ("rtl", "pysim"):
                shutil.rmtree(self.calib_dir / side, ignore_errors=True)
            self.corpus_path.unlink(missing_ok=True)
        if params:
            (self.calib_dir / "params.json").unlink(missing_ok=True)
            self._model._fitted = False


@dataclass
class StreamTimingModel(TimingModel):
    """A mem-stream component's residual model: basis ``[nwords, num_trans]`` — the counts the RTL
    law moves on (words moved, AXI bursts).  A pure-write component (``MemWStream``) injects the
    residual *trailing* (the posted-write drain), which the default ``placement`` reflects."""

    def __post_init__(self) -> None:
        if not self.features:
            self.features = ["nwords", "num_trans"]
        super().__post_init__()
