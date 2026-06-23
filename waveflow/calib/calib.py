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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

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
    forms; a single multi-target fit would be wrong)."""

    basis: list[str]
    target: str

    def __post_init__(self) -> None:
        self.basis = [str(b) for b in self.basis]
        self.target = str(self.target)
        self._fitted = False

    # -- data access --------------------------------------------------------
    @staticmethod
    def _frame(data) -> pd.DataFrame:
        """Accept a :class:`CalibDataFrame` or a raw ``DataFrame``; return the frame."""
        return data.df if isinstance(data, CalibDataFrame) else data

    def design(self, df: pd.DataFrame) -> np.ndarray:
        return df[self.basis].to_numpy(dtype=float)

    def targets(self, df: pd.DataFrame) -> np.ndarray:
        return df[self.target].to_numpy(dtype=float)

    # -- interface (subclass) ----------------------------------------------
    def fit(self, data) -> "CalibModel":
        raise NotImplementedError

    def _predict_one(self, row) -> float:
        raise NotImplementedError

    def predict(self, row) -> float:
        """Predict for a single row (a mapping of column -> value)."""
        if not self._fitted:
            raise RuntimeError(f"{type(self).__name__}({self.target!r}) is not fitted")
        return self._predict_one(row)

    # -- metrics ------------------------------------------------------------
    def score(self, data) -> float:
        """R² of the fitted model on *data* (a CalibDataFrame or DataFrame)."""
        df = self._frame(data)
        y = self.targets(df)
        pred = np.array([self.predict(r) for r in df.to_dict("records")], dtype=float)
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
            out.append(abs(self.predict(r) - actual) / abs(actual))
        return out

    def max_rel_error(self, data) -> float:
        errs = self.rel_errors(data)
        return max(errs) if errs else 0.0

    def holdout_report(self, train, test) -> dict:
        """Fit on *train*, report R² and held-out residuals on *test* (frames/CalibDataFrames).

        Returns ``{"target", "r2_train", "test": [{pred, actual, rel_err}...], "max_rel_err"}``.
        """
        self.fit(train)
        recs = []
        for r in self._frame(test).to_dict("records"):
            actual = float(r[self.target])
            pred = self.predict(r)
            rel = abs(pred - actual) / abs(actual) if actual else None
            recs.append({"pred": round(pred, 3), "actual": round(actual, 3),
                         "rel_err": round(rel, 4) if rel is not None else None})
        rels = [c["rel_err"] for c in recs if c["rel_err"] is not None]
        return {"target": self.target, "r2_train": round(self.score(train), 5),
                "test": recs, "max_rel_err": round(max(rels), 4) if rels else None}


# ---------------------------------------------------------------------------
# LinCalibModel — sklearn LinearRegression over the basis columns
# ---------------------------------------------------------------------------

@dataclass
class LinCalibModel(CalibModel):
    """Linear least-squares model over the :attr:`basis` columns, backed by
    ``sklearn.LinearRegression``.

    *fit_intercept* adds a constant term (sklearn fits it separately); pass
    ``fit_intercept=False`` for a through-origin model whose coefficients are the
    physical per-feature rates (e.g. FIR's ``span = setup·num_trans +
    per_word·nwords``).  Non-linear bases are caller-side derived columns
    (``df["sqrt_nc"] = df.n_col ** 0.5``) — the class is general; the *choice* of basis
    is the caller's.
    """

    fit_intercept: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        self._reg = None

    def fit(self, data) -> "LinCalibModel":
        from sklearn.linear_model import LinearRegression

        df = self._frame(data)
        X = self.design(df)
        y = self.targets(df)
        self._reg = LinearRegression(fit_intercept=self.fit_intercept).fit(X, y)
        self._fitted = True
        return self

    def _predict_one(self, row) -> float:
        x = np.array([[float(row[n]) for n in self.basis]], dtype=float)
        return float(self._reg.predict(x)[0])

    # -- introspection ------------------------------------------------------
    @property
    def coeffs(self) -> dict:
        """Fitted ``{column: coefficient}`` (plus ``"intercept"`` if fitted)."""
        if not self._fitted:
            raise RuntimeError("not fitted")
        d = {n: float(c) for n, c in zip(self.basis, self._reg.coef_)}
        if self.fit_intercept:
            d["intercept"] = float(self._reg.intercept_)
        return d

    def as_dict(self) -> dict:
        """Serializable model: ``{target, basis, coeffs, fit_intercept}``."""
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
        actual = self.targets(df)
        pred = np.array([self.predict(r) for r in df.to_dict("records")], dtype=float)
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

    def fit(self, data) -> "InterpCalibModel":
        df = self._frame(data)
        f = self.basis[0]
        xy: dict[float, list[float]] = {}
        for x, y in zip(df[f].to_numpy(dtype=float), df[self.target].to_numpy(dtype=float)):
            xy.setdefault(float(x), []).append(float(y))
        keys = sorted(xy)
        self._xs = np.array(keys, dtype=float)
        self._ys = np.array([float(np.mean(xy[k])) for k in keys], dtype=float)
        self._fitted = True
        return self

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

    @classmethod
    def from_samples(cls, feature: str, xs, ys, target: str = "") -> "InterpCalibModel":
        """Build directly from a stored ``(xs, ys)`` table (the deserialize path)."""
        m = cls([feature], target)
        m._xs = np.asarray(xs, dtype=float)
        m._ys = np.asarray(ys, dtype=float)
        m._fitted = True
        return m
