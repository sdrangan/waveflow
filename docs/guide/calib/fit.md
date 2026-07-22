---
title: Fitting a timing model
parent: Timing model fitting
nav_order: 1
audience: python
api: [CalibDataFrame, LinCalibModel]
summary: "The direct method: recover a loop timing model's parameters (latency, ii) from a sweep. Run the kernel at several input sizes, record (n, cycles) into a CalibDataFrame, and fit the LinCalibModel — the coefficients ARE latency and ii. Two points suffice in principle; use more and check R² / held-out error. unroll_factor reshapes the basis (ceil(n/U)), fixed from synthesis or swept. The (n, cycles) ground truth comes from a Vitis HLS cosim sweep."
---

# Fitting a timing model

The [loop timing model](../timing_model/loops.md) predicts `cycles = latency + ii·(m − 1)` from two
parameters. This page recovers those parameters **from measurement** — the *direct* method: run the
kernel at a range of sizes, record the cycles, and fit. (Reusable infra components use the
[residual fit](./component_residual.md) instead; this simple case comes first.)

## The sweep

The datapoints are a **sweep**: the kernel run at several input sizes `n`, each yielding one measured
`(n, cycles)` row. Those rows are the corpus — a [`CalibDataFrame`](./dataframe.md), one row per
measurement:

```python
from waveflow.calib.calib import CalibDataFrame

corpus = CalibDataFrame()
for n in (64, 128, 256, 512):
    cycles = measure(n)                       # one cosim/RTL run at size n (see below)
    corpus.add_datapoint({"n": n, "cycles": cycles})
```

## The fit

The loop model is linear in `latency` and `ii` — with the basis map `[1, m − 1]` (`m = ceil(n / U)`),
the two coefficients *are* the parameters (see [Timing models for loops](../timing_model/loops.md)).
Fitting is one call:

```python
import math
from waveflow.calib.calib import LinCalibModel

tm = LinCalibModel(
    basis=["const", "trip"], target="cycles",
    coeff_names=["latency", "ii"], fit_intercept=False,
    transform=lambda r: [1.0, math.ceil(r["n"] / U) - 1],
)
tm.fit(corpus)
print(tm.coeffs)                              # {"latency": …, "ii": …} — recovered from the sweep
```

Two points suffice in principle (two unknowns); use more and a least-squares fit averages out
measurement noise. Then check the model **generalizes** rather than merely interpolates — hold a size
out and measure the error on it:

```python
report = tm.holdout_report(train=corpus_wo_512, test=corpus_512)   # R² + held-out rel error
```

## Recovering `unroll_factor`

`unroll_factor` (`U`) is not a fitted coefficient — it reshapes the basis through `m = ceil(n / U)`.
Two ways to pin it down:

1. **Known from synthesis** — you set the unroll pragma, so plug `U` in and fit only `latency`, `ii`.
2. **Swept** — fit for each candidate `U`, pick the best R². The throughput asymptote also reveals it:
   at large `n`, `cycles / n → ii / U`.

## Where the data comes from

The `(n, cycles)` points are **ground truth**, and the faithful source is a **Vitis HLS cosim sweep**:
synthesize the kernel, run cosim at each size, and read the cycle count. That is a cycle-timed
measurement ([LT vs CT](../timing_model/models.md)) used to calibrate the loosely-timed model so the
fast LT sim predicts the slow RTL. See [cosim timing](../timing/cosim_timing.md) for extracting the
counts.

The line-fit here uses a [`LinCalibModel`](./models.md); for a smooth, saturating curve that no line
captures, an [`InterpCalibModel`](./models.md) is a calibrated lookup instead (see
[the worked example](./example.md)).

## See also

- [Timing models for loops](../timing_model/loops.md) — the `latency + ii·(m − 1)` model this fits.
- [Models](./models.md) / [The corpus — `CalibDataFrame`](./dataframe.md) — the `LinCalibModel` and
  corpus this uses.
- [A worked example](./example.md) — the primitive fit mechanics (score, holdout, `InterpCalibModel`).
- [Component residuals](./component_residual.md) — the *residual* fitting method, for reusable infra.
- [Timing Analysis Tools — cosim timing](../timing/cosim_timing.md) — the measurement side of the fit.
