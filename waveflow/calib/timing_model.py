"""timing_model.py — per-component timing calibration, orchestration over :mod:`calib`.

:mod:`waveflow.calib.calib` has the *model* machinery (fit / predict / state_dict / seed) but, as its
own docstring says, "no infrastructure for collecting data".  :class:`TimingModel` is that missing
layer: it collects a component's per-firing measurements from an RTL trace and from pysim, fits the
**residual** (the delay pysim is missing) with a composed :class:`~waveflow.calib.calib.CalibModel`,
and exposes a ``predict`` the component's ``run_iter`` calls.

See ``plans/timing_model.md``.  Two properties are load-bearing and easy to get wrong:

* **The target is the residual, corrected for the current prediction.**  The pysim span already
  includes whatever the model predicted on that run, so the fit target is
  ``rtl_span - pysim_span + current_dly``.  This makes the fit self-correcting: any run is a valid
  datapoint (no model-disabled baseline needed), and calibration converges iteratively from a
  0-seed.
* **Validity is ``blocked == 0``.**  A firing that stalled on a full downstream channel measured
  contention, not the component's own cost; those rows are dropped from the fit.  ``ExtractBurstsStep``
  already computes ``blocked`` from FIFO occupancy.

Everything is kept in **cycles** (the RTL table is in cycles; the component records its pysim spans
and ``current_dly`` in cycles too), so the fit is clock-independent and ``predict`` returns cycles —
the caller multiplies by ``clk.period`` for the ``timeout``.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from waveflow.calib.calib import CalibDataFrame, LinCalibModel

#: Fit-target column name (the residual, in cycles).
RESIDUAL = "residual"


@dataclass
class TimingModel:
    """A component's timing residual model plus the corpus that fits it.

    Parameters
    ----------
    component : str
        The identifier this component's firings carry in the timing table — the task-body ``id``
        (``"mem_w_stream_framed_done_task"``) or the RTL ``inst``.  :meth:`collect_rtl` filters the
        table by it.
    features : list[str]
        The regression basis, columns present in *both* the RTL table and the pysim firings
        (``["nwords", "num_trans"]``).  ``predict`` takes a row over these.
    calib_dir : str | Path
        The corpus + artifact directory.  Per-run folders under ``runs/``; the fitted params at
        ``params.json`` (see the module plan for the layout and why per-run beats a shared csv).
    num_targets : int
        How many delays ``predict`` returns (default 1).  ``> 1`` is the API for injecting a delay
        *between* internal stages, but needs per-stage measurement (Tier-2 tracing), so every
        operation raises ``NotImplementedError`` for now.
    placement : str
        ``"leading"`` | ``"trailing"`` — where ``run_iter`` injects the delay.  Advisory metadata;
        it does not change the fit, but records the intent (a writer's residual is a trailing
        posted-write drain, a reader's is leading control setup).
    seed : dict | None
        Seed params for the model (its ``state_dict``), used by ``predict`` before any fit — so a
        fresh component still simulates.  ``None`` seeds to zero additional delay.
    """

    component: str
    calib_dir: str | Path
    #: The regression basis.  Required in the base; a subclass (e.g. StreamTimingModel) may default
    #: it — hence declared with a `None` sentinel so field order allows the override.
    features: list[str] | None = None
    num_targets: int = 1
    placement: str = "trailing"
    seed: dict | None = None
    _model: LinCalibModel = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.features:
            raise ValueError(f"{type(self).__name__} requires `features` (the regression basis)")
        self.calib_dir = Path(self.calib_dir)
        self.features = [str(f) for f in self.features]
        # A zero seed = "no additional delay" until calibrated: intercept 0, all coeffs 0.
        seed = self.seed if self.seed is not None else {
            **{f: 0.0 for f in self.features}, "intercept": 0.0}
        self._model = LinCalibModel(
            basis=self.features, target=RESIDUAL, fit_intercept=True,
            coeff_names=self.features, seed=seed, path=self.calib_dir / "params.json")

    # -- paths --------------------------------------------------------------
    @property
    def runs_dir(self) -> Path:
        return self.calib_dir / "runs"

    def _run_dir(self, run_id: str) -> Path:
        d = self.runs_dir / str(run_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _guard_single(self) -> None:
        if self.num_targets != 1:
            raise NotImplementedError(
                f"num_targets={self.num_targets}: multi-target calibration needs per-stage "
                f"measurement (Tier-2 intra-component tracing). The API is carried; the "
                f"implementation is single-target for now.")

    # -- collection ---------------------------------------------------------
    def collect_rtl(self, events: dict, run_id: str) -> Path:
        """Write this component's firings from an ``ExtractBurstsStep`` *events* dict into
        ``runs/<run_id>/rtl_firings.csv``.  Keeps ALL firings (``blocked`` included) — the fit
        filters; the full set is the debug trail.  Overwrites the run folder's file, so re-running
        the same scenario replaces rather than duplicates."""
        self._guard_single()
        rows = [f for f in events.get("firings", []) if f["component"] == self.component]
        df = pd.DataFrame(rows)
        out = self._run_dir(run_id) / "rtl_firings.csv"
        df.to_csv(out, index=False)
        return out

    def collect_pysim(self, firings: list[dict], run_id: str) -> Path:
        """Write this component's pysim firings into ``runs/<run_id>/pysim_firings.csv``.

        Each firing is a mapping with the *features*, ``span`` (cycles), and ``current_dly`` (cycles
        — what :meth:`predict` returned on this run, so the fit can subtract it back out).  The
        component produces these from its own per-firing instrumentation."""
        self._guard_single()
        df = pd.DataFrame(firings)
        missing = ({"span", "current_dly", *self.features}) - set(df.columns)
        if firings and missing:
            raise ValueError(f"pysim firing rows are missing {sorted(missing)}; got {list(df.columns)}")
        out = self._run_dir(run_id) / "pysim_firings.csv"
        df.to_csv(out, index=False)
        return out

    # -- fit ----------------------------------------------------------------
    def _load_all(self, name: str) -> pd.DataFrame:
        """Concat ``runs/*/<name>`` (the per-run source of truth) into one frame."""
        frames = [pd.read_csv(p) for p in sorted(self.runs_dir.glob(f"*/{name}")) if p.stat().st_size]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def build_corpus(self) -> CalibDataFrame:
        """Join RTL and pysim firings into the residual corpus: one row per feature point.

        RTL firings are filtered to ``blocked == 0`` (a stalled firing measured contention, not the
        component's cost).  Both sides are aggregated by the *features* (16 jobs at one ``nwords``
        collapse to one point — the median span), then joined so the target is
        ``rtl_span - pysim_span + current_dly``."""
        self._guard_single()
        rtl, py = self._load_all("rtl_firings.csv"), self._load_all("pysim_firings.csv")
        corpus = CalibDataFrame(columns=[*self.features, RESIDUAL])
        if rtl.empty or py.empty:
            return corpus

        valid = rtl[rtl["blocked"] == 0]
        rtl_g = valid.groupby(self.features)["span"].median()
        py_g = py.groupby(self.features)[["span", "current_dly"]].median()

        for key, rtl_span in rtl_g.items():
            if key not in py_g.index:
                continue                        # a feature point pysim never ran — no residual
            feat = key if isinstance(key, tuple) else (key,)
            row = {f: float(v) for f, v in zip(self.features, feat)}
            row[RESIDUAL] = float(rtl_span) - float(py_g.loc[key, "span"]) \
                + float(py_g.loc[key, "current_dly"])
            corpus.add_datapoint(row)
        return corpus

    def fit(self) -> "TimingModel":
        """Build the corpus and fit the residual model; write ``params.json``.  Raises if the corpus
        is empty (nothing joined) so a silent no-op fit cannot leave a stale/seed model looking
        fitted."""
        self._guard_single()
        corpus = self.build_corpus()
        if len(corpus) == 0:
            raise RuntimeError(
                f"{self.component}: no residual datapoints — RTL and pysim firings did not join on "
                f"{self.features} (collect both, and check `blocked == 0` firings exist).")
        self._model.fit(corpus)
        self._model.save_model()
        return self

    # -- deploy -------------------------------------------------------------
    def predict(self, row: dict) -> list[float]:
        """Additional delay(s), in **cycles**, for a firing described by *row* (feature → value).

        Returns a list of length :attr:`num_targets`.  A never-fitted model falls back to the seed
        (``load_or_default``), so this is safe to call before any calibration — it just predicts the
        seed delay (0 by default)."""
        self._guard_single()
        if not self._model._fitted:
            self._model.load_or_default()
        return [max(0.0, self._model.predict(row))]

    # -- lifecycle ----------------------------------------------------------
    def reset(self, runs: bool = True, params: bool = False) -> None:
        """Wipe the corpus and/or the fitted params, to recalibrate from scratch.

        ``runs=True`` (default) clears ``runs/`` — the common "re-sweep from scratch".
        ``params=True`` deletes ``params.json`` so the next :meth:`predict` falls back to the seed.
        The default leaves a fitted model deployable until the next :meth:`fit` replaces it."""
        if runs and self.runs_dir.exists():
            shutil.rmtree(self.runs_dir)
        if params:
            (self.calib_dir / "params.json").unlink(missing_ok=True)
            self._model._fitted = False


@dataclass
class StreamTimingModel(TimingModel):
    """A mem-stream component's residual model: basis ``[nwords, num_trans]``.

    The two features are the ones the RTL law moves on — ``nwords`` (words moved) and ``num_trans``
    (AXI bursts).  A pure-write component (``MemWStream``) injects the residual *trailing* (the
    posted-write drain); the default ``placement`` reflects that.  ``num_targets`` stays 1 until a
    stream component needs a per-stage split.
    """

    def __post_init__(self) -> None:
        if not self.features:
            self.features = ["nwords", "num_trans"]
        super().__post_init__()
