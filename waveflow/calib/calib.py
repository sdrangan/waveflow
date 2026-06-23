"""calib.py — bare-bones calibration corpus + linear model fitting.

The pieces a timing calibration recurs on (FIR, VMAC, every future cycle model):

* :class:`CalibDatabase` — a list of datapoint rows (``dict`` of named variables),
  one per synth/cosim measurement.  This *is* the structured timing-artifact corpus.
* :class:`Feature` — one named design-matrix column: a raw variable or a transform
  (``sqrt``, products, …) of the row.  Lets a model carry basis functions while the
  database stays a flat table of measurements.
* :class:`CalibModel` — the fit / predict / score interface.  Per-target (one model
  per ``read_span`` / ``write_span`` / ``compute`` / ``fill``): targets have
  different forms, so a single multi-target fit is wrong.
* :class:`LinCalibModel` — ``sklearn.LinearRegression`` over a list of
  :class:`Feature`, with held-out error, R² and a simple plot.

Intentionally minimal: an sklearn wrapper, not an ML framework.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import numpy as np

#: A row is a flat mapping of variable name -> measured value.
Row = dict


# ---------------------------------------------------------------------------
# Feature — one named design-matrix column (raw variable or a transform of a row)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Feature:
    """One design-matrix column: ``name`` and a ``fn(row) -> float``.

    Construct from a bare variable name (``Feature("nwords")`` → ``row["nwords"]``)
    or with an explicit transform (``Feature("sqrt_nc", lambda r: r["n_col"] ** 0.5)``)
    for basis functions / products.  :meth:`coerce` accepts either form so models take
    ``["num_trans", "nwords"]`` *or* ``[Feature(...), ...]`` interchangeably.
    """

    name: str
    fn: Callable[[Row], float] | None = None

    def value(self, row: Row) -> float:
        if self.fn is not None:
            return float(self.fn(row))
        return float(row[self.name])

    @staticmethod
    def coerce(spec: "str | Feature | tuple[str, Callable]") -> "Feature":
        if isinstance(spec, Feature):
            return spec
        if isinstance(spec, str):
            return Feature(spec)
        if isinstance(spec, tuple) and len(spec) == 2:
            return Feature(spec[0], spec[1])
        raise TypeError(f"cannot coerce {spec!r} to a Feature")


def _coerce_basis(basis: Iterable) -> list[Feature]:
    return [Feature.coerce(b) for b in basis]


# ---------------------------------------------------------------------------
# CalibDatabase — the structured datapoint corpus
# ---------------------------------------------------------------------------

@dataclass
class CalibDatabase:
    """A table of calibration datapoints — one ``dict`` row per synth/cosim measurement.

    *var_names* are the columns the database expects (independent variables **and**
    measured targets); they fix the :meth:`display` column order.  Rows may carry
    extra keys (ignored by ordering) and need not provide every column.
    """

    var_names: list[str]
    rows: list[Row] = field(default_factory=list)

    def add_datapoint(self, point: Row) -> None:
        """Append one datapoint (a mapping of variable name -> value)."""
        self.rows.append(dict(point))

    def extend(self, points: Iterable[Row]) -> None:
        for p in points:
            self.add_datapoint(p)

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self):
        return iter(self.rows)

    def filter(self, pred: Callable[[Row], bool]) -> "CalibDatabase":
        """A new database with the rows for which *pred* is true (e.g. a fit/holdout split)."""
        db = CalibDatabase(list(self.var_names))
        db.rows = [r for r in self.rows if pred(r)]
        return db

    def column(self, name: str) -> np.ndarray:
        return np.array([float(r[name]) for r in self.rows], dtype=float)

    def display(self):
        """Return the corpus as a ``pandas.DataFrame`` (ordered by :attr:`var_names`)."""
        import pandas as pd

        df = pd.DataFrame(self.rows)
        cols = [c for c in self.var_names if c in df.columns]
        extra = [c for c in df.columns if c not in cols]
        return df[cols + extra] if len(df.columns) else df


# ---------------------------------------------------------------------------
# CalibModel — per-target fit / predict / score
# ---------------------------------------------------------------------------

@dataclass
class CalibModel:
    """Base per-target calibration model: predict ``target`` from a ``basis`` of features.

    Subclasses implement :meth:`fit` and :meth:`_predict_one`.  One model per target —
    ``read_span``, ``write_span``, ``compute``, ``fill`` each get their own (different
    forms; a single multi-target fit would be wrong)."""

    basis: list[Feature]
    target: str

    def __post_init__(self) -> None:
        self.basis = _coerce_basis(self.basis)
        self._fitted = False

    # -- design matrix ------------------------------------------------------
    def design(self, rows: Sequence[Row]) -> np.ndarray:
        return np.array([[f.value(r) for f in self.basis] for r in rows], dtype=float)

    def targets(self, rows: Sequence[Row]) -> np.ndarray:
        return np.array([float(r[self.target]) for r in rows], dtype=float)

    # -- interface (subclass) ----------------------------------------------
    def fit(self, data) -> "CalibModel":
        raise NotImplementedError

    def _predict_one(self, row: Row) -> float:
        raise NotImplementedError

    def predict(self, row: Row) -> float:
        if not self._fitted:
            raise RuntimeError(f"{type(self).__name__}({self.target!r}) is not fitted")
        return self._predict_one(row)

    # -- metrics ------------------------------------------------------------
    @staticmethod
    def _rows(data) -> list[Row]:
        return list(data.rows) if isinstance(data, CalibDatabase) else list(data)

    def score(self, data) -> float:
        """R² of the fitted model on *data* (a database or rows)."""
        rows = self._rows(data)
        y = self.targets(rows)
        pred = np.array([self.predict(r) for r in rows], dtype=float)
        ss_res = float(np.sum((y - pred) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        return 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0

    def rel_errors(self, data) -> list[float]:
        """Per-row ``|pred - actual| / |actual|`` on *data* (skips actual==0)."""
        out = []
        for r in self._rows(data):
            actual = float(r[self.target])
            if actual == 0:
                continue
            out.append(abs(self.predict(r) - actual) / abs(actual))
        return out

    def max_rel_error(self, data) -> float:
        errs = self.rel_errors(data)
        return max(errs) if errs else 0.0

    def holdout_report(self, train, test) -> dict:
        """Fit on *train*, report R² and held-out residuals on *test* (both databases/rows).

        Returns ``{"r2_train", "test": [{features, pred, actual, rel_err}...], "max_rel_err"}``.
        """
        self.fit(train)
        rows = self._rows(test)
        recs = []
        for r in rows:
            actual = float(r[self.target])
            pred = self.predict(r)
            rel = abs(pred - actual) / abs(actual) if actual else None
            recs.append({"pred": round(pred, 3), "actual": round(actual, 3),
                         "rel_err": round(rel, 4) if rel is not None else None})
        rels = [c["rel_err"] for c in recs if c["rel_err"] is not None]
        return {"target": self.target, "r2_train": round(self.score(train), 5),
                "test": recs, "max_rel_err": round(max(rels), 4) if rels else None}


# ---------------------------------------------------------------------------
# LinCalibModel — sklearn LinearRegression over a Feature basis
# ---------------------------------------------------------------------------

@dataclass
class LinCalibModel(CalibModel):
    """Linear least-squares model over the :attr:`basis`, backed by
    ``sklearn.LinearRegression``.

    *fit_intercept* adds a constant term (sklearn fits it separately); pass
    ``fit_intercept=False`` for a through-origin model whose coefficients are the
    physical per-feature rates (e.g. FIR's ``span = setup·num_trans +
    per_word·nwords``).  Basis functions/products go in via :class:`Feature`
    transforms — the class is general; the *choice* of basis is the caller's.
    """

    fit_intercept: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        self._reg = None

    def fit(self, data) -> "LinCalibModel":
        from sklearn.linear_model import LinearRegression

        rows = self._rows(data)
        if len(rows) < len(self.basis) + (1 if self.fit_intercept else 0):
            # Under-determined — still fit (least-norm), but the caller should know.
            pass
        X = self.design(rows)
        y = self.targets(rows)
        self._reg = LinearRegression(fit_intercept=self.fit_intercept).fit(X, y)
        self._fitted = True
        return self

    def _predict_one(self, row: Row) -> float:
        x = np.array([[f.value(row) for f in self.basis]], dtype=float)
        return float(self._reg.predict(x)[0])

    # -- introspection ------------------------------------------------------
    @property
    def coeffs(self) -> dict:
        """Fitted ``{feature_name: coefficient}`` (plus ``"intercept"`` if fitted)."""
        if not self._fitted:
            raise RuntimeError("not fitted")
        d = {f.name: float(c) for f, c in zip(self.basis, self._reg.coef_)}
        if self.fit_intercept:
            d["intercept"] = float(self._reg.intercept_)
        return d

    def as_dict(self) -> dict:
        """Serializable model: ``{target, basis names, coeffs, intercept, fit_intercept}``."""
        return {"target": self.target, "basis": [f.name for f in self.basis],
                "coeffs": self.coeffs, "fit_intercept": self.fit_intercept}

    def plot(self, data, x_name: str, ax=None, label: str | None = None):
        """Scatter actual vs. fitted against the *x_name* variable (simple diagnostic).

        Returns the matplotlib ``Axes``.  Import is local so the package has no hard
        matplotlib dependency."""
        import matplotlib.pyplot as plt

        rows = self._rows(data)
        xs = np.array([float(r[x_name]) for r in rows], dtype=float)
        order = np.argsort(xs)
        actual = self.targets(rows)
        pred = np.array([self.predict(r) for r in rows], dtype=float)
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
    """Piecewise-linear interpolation over a **single** feature — a calibrated *lookup*, not a
    curve fit.

    For a physical quantity that is genuinely non-linear but smooth and **saturating** (e.g. a
    per-row pipeline / ping-pong depth as a function of row length): sample it densely enough
    that linear interpolation between samples is clean, and **clamp (flat-extrapolate) beyond
    the sampled range** — exactly the saturation behaviour.  This is the principled alternative
    to forcing a wrong basis (a ``sqrt`` fudge) onto a measured curve: carry the measurement.

    Duplicate feature values are averaged (e.g. ``g(n_col)`` measured at several ``n_row``).
    """

    def __post_init__(self) -> None:
        super().__post_init__()
        if len(self.basis) != 1:
            raise ValueError("InterpCalibModel supports exactly one feature")
        self._xs = None
        self._ys = None

    def fit(self, data) -> "InterpCalibModel":
        rows = self._rows(data)
        f = self.basis[0]
        xy: dict[float, list[float]] = {}
        for r in rows:
            xy.setdefault(f.value(r), []).append(float(r[self.target]))
        xs = sorted(xy)
        self._xs = np.array(xs, dtype=float)
        self._ys = np.array([float(np.mean(xy[x])) for x in xs], dtype=float)
        self._fitted = True
        return self

    def _predict_one(self, row: Row) -> float:
        # np.interp clamps to the endpoint values outside [xs[0], xs[-1]] — the saturation.
        return float(np.interp(self.basis[0].value(row), self._xs, self._ys))

    @property
    def samples(self) -> dict:
        """The calibrated table ``{"feature", "x", "y"}`` (serializable)."""
        if not self._fitted:
            raise RuntimeError("not fitted")
        return {"feature": self.basis[0].name,
                "x": [float(v) for v in self._xs], "y": [float(v) for v in self._ys]}

    @classmethod
    def from_samples(cls, feature: "str | Feature", xs, ys, target: str = "") -> "InterpCalibModel":
        """Build directly from a stored ``(xs, ys)`` table (the deserialize path)."""
        m = cls([feature], target)
        m._xs = np.asarray(xs, dtype=float)
        m._ys = np.asarray(ys, dtype=float)
        m._fitted = True
        return m
