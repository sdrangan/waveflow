---
title: A worked example
parent: Model calibration
nav_order: 6
audience: python
api: [CalibDataFrame, LinCalibModel, InterpCalibModel]
summary: "A minimal end-to-end calibration: build a CalibDataFrame from a few (size, cycles) measurements, fit a LinCalibModel, read its coefficients, score it, hold a point out, and plot it; then calibrate a saturating curve with InterpCalibModel."
---

# A worked example

A self-contained walk through the calibration loop on a tiny synthetic dataset. Everything here runs
against the real package.

## 1. Collect the corpus

Suppose a kernel's measured cycle count is affine in an effective trip count `m` (a block-processing
kernel: `cycles = latency + ii·m`). Each cosim run is one datapoint:

```python
from waveflow.calib import CalibDataFrame, LinCalibModel

db = CalibDataFrame(columns=["m", "cycles"])
db.extend([
    {"m": 1, "cycles": 52},
    {"m": 2, "cycles": 64},
    {"m": 4, "cycles": 88},
    {"m": 8, "cycles": 136},
])
print(db.df)            # a pandas DataFrame (with a measured_at column)
```

> **These `(m, cycles)` numbers are synthetic** — chosen to make the fit easy to follow. In a real
> calibration you don't type the cycle counts; you **capture them from cosim**: instrument the design,
> run an RTL co-simulation at each size, and read the cycle count off the VCD. That end-to-end loop —
> where the datapoints come from — is the [cosim sweep](../timing_model/fit.md#where-the-data-comes-from), extracted
> with the [Timing Analysis Tools](../timing/cosim_timing.md).

## 2. Fit, inspect, predict, score

```python
model = LinCalibModel(basis=["m"], target="cycles").fit(db)

print(model.coeffs)             # {'m': 12.0, 'intercept': 40.0}  ->  ii = 12, latency = 40
print(model.predict({"m": 3}))  # 76.0
print(model.score(db))          # 1.0  (R² — exact line here)
```

`coeffs` recovers the physical numbers: the slope is the initiation interval `ii`, the intercept is
the `latency` (see [Fitting a timing model](../timing_model/fit.md)).

## 3. Hold a point out (does it generalize?)

`score` on the training data only tells you the fit *interpolates its own points*. To know the model
*generalizes*, fit on a subset and report the held-out residual:

```python
train = db.df[db.df.m != 4]      # native pandas on .df
test  = db.df[db.df.m == 4]

report = LinCalibModel(basis=["m"], target="cycles").holdout_report(train, test)
print(report)
# {'target': 'cycles', 'r2_train': 1.0,
#  'test': [{'pred': 88.0, 'actual': 88.0, 'rel_err': 0.0}], 'max_rel_err': 0.0}
```

## 4. Plot (actual vs. fitted)

```python
import matplotlib.pyplot as plt

ax = model.plot(db, x_name="m")   # scatter of actuals + the fitted line
plt.show()
```

`plot` returns the matplotlib `Axes`, so you can pass your own `ax=` to overlay several targets.

## 5. A saturating curve with `InterpCalibModel`

Not every quantity is a line. A per-row pipeline depth grows with the row length and then *saturates*
— a line or a `sqrt` would both be wrong. Carry the measurement as a calibrated lookup instead:

```python
from waveflow.calib import InterpCalibModel

depth = CalibDataFrame(columns=["n_col", "row_depth"])
depth.extend([
    {"n_col": 64,   "row_depth": 70.0},
    {"n_col": 256,  "row_depth": 260.0},
    {"n_col": 1024, "row_depth": 268.0},   # saturated
])

g = InterpCalibModel(basis=["n_col"], target="row_depth").fit(depth)
print(g.predict({"n_col": 128}))    # ≈ 133.3  (interpolated between 70 and 260)
print(g.predict({"n_col": 4096}))   # 268.0    (clamped past the last sample — the saturation)
print(g.samples)                    # {'feature': 'n_col', 'x': [...], 'y': [...]}
```

That is the stance for a smooth, saturating physical curve — a per-row pipeline depth, a fill
latency — that a straight line would distort: **measure** it rather than force a wrong fit.

## See also

- [The corpus — `CalibDataFrame`](./dataframe.md) — building and persisting the measurement table.
- [Models](./models.md) — the full method reference for `CalibModel` / `LinCalibModel` / `InterpCalibModel`.
- [Timing model fitting](./index.md) — the overall workflow.
- [Fitting a timing model](../timing_model/fit.md) — the direct sweep-based fit this example's mechanics support.
