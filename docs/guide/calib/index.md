---
title: Calibration
parent: Guide
nav_order: 12
has_children: true
audience: python
api: [CalibDataFrame, CalibModel, LinCalibModel, InterpCalibModel]
summary: "The waveflow.calib package: a small, reusable corpus + model layer for fitting physically-reasonable timing and resource models from synth/cosim measurements. A CalibDataFrame holds one row per measurement; per-target models (LinCalibModel, InterpCalibModel) fit, predict, score, and report held-out error — deliberately a DataFrame wrapper plus sklearn/interp models, not an ML framework."
---

# Calibration

A Waveflow component's timing model is **loosely-timed** (see [Timing Models](../timing_model/)): it
predicts a timeline from a few numbers — a latency, an initiation interval, a per-row depth. Those
numbers are properties of the *synthesized* hardware. **Calibration** is the discipline of *fitting
them from measurement* — running the kernel through synthesis or RTL cosim at a range of sizes,
recording the resulting cycle counts (and resource usage), and fitting a model so the fast LT
simulation tracks the slow RTL without you transcribing report numbers by hand.

The same machinery calibrates **timing** (cycles vs. size) and **resources** (LUT/FF/BRAM vs. a
parameter) — both are "fit a small model to a table of measurements."

The `waveflow.calib` package is deliberately **bare-bones**: a thin `pandas.DataFrame` wrapper for the
corpus, plus sklearn-backed and interpolation models with held-out-error / R² / plot helpers. It is
*not* an ML framework — the models are small and physically motivated.

## The workflow

```
synth / cosim sweep   →   CalibDataFrame   →   fit a per-target model   →   predict in the LT sim
 (one row per run)        (the corpus)         (Lin / Interp)               (and validate held-out)
```

1. **Measure.** Run the kernel at several sizes; each run is one datapoint (features like `n_row`,
   `n_col`, and measured targets like `cycles` or `bram`).
2. **Collect** the runs into a `CalibDataFrame` — the structured corpus, one row per measurement.
3. **Fit** a [model](./models.md) per target. Targets have different shapes, so each gets its own
   model (a linear fit for an affine cycle count, a calibrated lookup for a saturating curve).
4. **Predict** inside the component's timing model, and **validate** on a held-out point so you know
   the fit *generalizes* rather than memorizes.

## The pieces

The corpus and the models are documented on their own pages. In brief: the corpus is a
[`CalibDataFrame`](./dataframe.md) — a thin `pandas.DataFrame` wrapper holding one timestamped row per
synth/cosim measurement; the [models](./models.md) fit a single target (e.g. `cycles`) from a basis of
its columns.

## In this section

- [The corpus — `CalibDataFrame`](./dataframe.md) — one timestamped row per measurement, a
  `pandas.DataFrame` under `.df`, with `save` / `load`.
- [Models](./models.md) — the per-target fit / predict / score interface (`CalibModel`), the linear
  model (`LinCalibModel`), and the calibrated lookup (`InterpCalibModel`).
- [A worked example](./example.md) — fit a line to `(size, cycles)`, score it, plot it, hold a point
  out; then a saturating curve with `InterpCalibModel`.
- [Instrumenting a calibration](./instrumentation.md) — the playbook for collecting *real* data:
  where to log events in the sim, how to extract timing from a cosim sweep, and how to feed the
  fitted model back in (worked against the FIR example).

## See also

- [Fitting a timing model](../timing_model/fit.md) — the conceptual fit (recovering `latency` / `ii`
  from a line) that `LinCalibModel` performs.
- [Timing Analysis Tools](../timing/) — the *measurement* side: extracting cycle counts and bus spans
  from a VCD / cosim run (where the datapoints come from).
- [`examples/rowwise_fir`](../../../examples/rowwise_fir/fir_calibrate.py) — the richest worked case:
  a physical, near-fit-free decomposition with a single calibrated `InterpCalibModel` curve.
