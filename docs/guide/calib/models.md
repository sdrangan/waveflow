---
title: Models
parent: Calibration
nav_order: 7
audience: python
api: [CalibModel, LinCalibModel, InterpCalibModel]
summary: "The per-target calibration models. CalibModel is the fit / predict / score / holdout interface over column-name basis and target. LinCalibModel is an sklearn LinearRegression with coeffs, plot, and a through-origin knob. InterpCalibModel is a calibrated 1-D lookup (a measured table, not a curve fit) for smooth, saturating physical curves."
---

# Models

A calibration model predicts **one target** (e.g. `cycles`) from a **basis** of feature columns (e.g.
`["n_row", "nwords"]`). Each target gets its **own** model — different targets have different shapes,
so a single multi-target fit would be wrong. Basis and target are **column-name strings**; a model
reads them straight off a [`CalibDataFrame`](./dataframe.md) (or a raw `DataFrame`).

## `CalibModel` — the interface

[`CalibModel(basis, target)`](../../../waveflow/calib/calib.py) is the base class. You use a concrete
subclass (`LinCalibModel` / `InterpCalibModel`), but they all share this interface:

| method | purpose |
|---|---|
| `fit(data)` | fit on a `CalibDataFrame` / `DataFrame`; returns `self` |
| `predict(row)` | predict for one row — a mapping of column → value, e.g. `{"n_row": 4, "n_col": 256}` |
| `score(data)` | R² of the fitted model on `data` |
| `rel_errors(data)` | per-row `|pred − actual| / |actual|` (skips `actual == 0`) |
| `max_rel_error(data)` | the worst of those |
| `holdout_report(train, test)` | fit on `train`; report `r2_train` + per-row residuals on `test` |

`holdout_report` is the one to reach for when you care about *generalization*: fit on most of the
grid, hold a point out, and read its relative error.

## `LinCalibModel` — linear least squares

[`LinCalibModel(basis, target, fit_intercept=True)`](../../../waveflow/calib/calib.py) is an
`sklearn.LinearRegression` over the basis columns.

```python
from waveflow.calib import LinCalibModel

m = LinCalibModel(basis=["m"], target="cycles").fit(db)   # cycles ≈ intercept + b·m
m.coeffs          # {"m": <slope>, "intercept": <intercept>}
m.predict({"m": 9})
m.score(db)       # R²
m.as_dict()       # serializable {target, basis, coeffs, fit_intercept}
m.plot(db, x_name="m")   # scatter actual vs. fitted line; returns a matplotlib Axes
```

- **`coeffs`** is the fitted `{column: coefficient}` (plus `"intercept"` when `fit_intercept=True`).
- **`fit_intercept=False`** gives a *through-origin* model whose coefficients are the physical
  per-feature rates — e.g. a bus span `setup·num_trans + per_word·nwords` where the two coefficients
  *are* the setup and per-word costs.
- **Non-linear bases are caller-side derived columns**, not a model feature: if you need a `sqrt`
  term, add `db.df["sqrt_nc"] = db.df.n_col ** 0.5` and put `"sqrt_nc"` in the basis. The model stays
  a plain linear fit; the *choice* of basis is yours. (But prefer `InterpCalibModel` to forcing a
  wrong basis onto a measured curve — see below.)

## `InterpCalibModel` — a calibrated lookup

[`InterpCalibModel(basis, target)`](../../../waveflow/calib/calib.py) is piecewise-linear
interpolation over a **single** basis column — a calibrated *lookup*, not a curve fit. It is the
right tool for a quantity that is genuinely non-linear but **smooth and saturating** (e.g. a per-row
pipeline / ping-pong depth as a function of row length): sample it densely enough that linear
interpolation between samples is clean, and it **clamps (flat-extrapolates) beyond the sampled
range** — exactly the saturation behaviour.

```python
from waveflow.calib import InterpCalibModel

g = InterpCalibModel(basis=["n_col"], target="row_depth").fit(db)
g.predict({"n_col": 128})   # interpolated between samples; clamped past the ends (saturation)
g.samples                   # {"feature": "n_col", "x": [...], "y": [...]} — the calibrated table

# or build directly from a stored table (the deserialize path):
g2 = InterpCalibModel.from_samples("n_col", xs=[64, 256, 1024], ys=[69.5, 260.3, 268.5], target="row_depth")
```

Duplicate feature values are **averaged** — so a curve `row_depth(n_col)` measured at several `n_row`
collapses to one value per `n_col`. This is the principled alternative to a `sqrt` fudge: rather than
forcing a basis function the data doesn't obey, you *carry the measurement*.

## Choosing a model

- The target is **affine in its features** (a latency + a per-iteration cost) → `LinCalibModel`.
- The target is a **smooth, saturating 1-D curve** you'd rather measure than fit → `InterpCalibModel`.
- The target is **deterministic** (one transfer beat per word) → you don't need a model at all; carry
  the constant. (The matrix-LT FIR uses all three stances: deterministic occupancy, an exact-II=1
  compute, and *one* `InterpCalibModel` for the per-row depth.)

## See also

- [A worked example](./example.md) — `LinCalibModel` and `InterpCalibModel` end-to-end.
- [Fitting a timing model](../timing_model/fit.md) — the latency/`ii` line `LinCalibModel` recovers.
- [`waveflow/calib/calib.py`](../../../waveflow/calib/calib.py) — the source (≈300 lines).
