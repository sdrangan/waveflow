---
title: Fitting the model
parent: Rowwise FIR
nav_order: 8
has_children: false
audience: python
api: [InterpCalibModel, CalibDataFrame]
summary: "Fitting the FIR timing model from the cosim corpus: channel occupancy and II=1 compute need no fit, so the only calibrated term is row_depth(n_col) — a saturating InterpCalibModel lookup. The composed loosely-timed model then reproduces the RTL whole-kernel to ≤1.3% across the grid (0.11% on the interior held-out point, ≤0.60% on untrained columns). Shown graphically; the figure regenerates without Vitis."
---

# Fitting the model

The [corpus](./cosim_timing.md) is 12 ground-truth `(n_row, n_col) → cycles` rows. Fitting turns it into
the [timing model](./timing_model.md). The headline is how *little* is fit.

## Fit the primitives, not the whole-kernel

The discipline is to fit each physical primitive, never the emergent whole-kernel directly (that would
manufacture a fudge term — see [the playbook](../../guide/calib/)). For FIR three of
the four terms need **no fit at all**:

- **Channel occupancy** — read off the [transfer-beat counts](./cosim_timing.md): exactly `nwords`,
  slope 1. Deterministic.
- **Compute body** — `trips + (n_row−1)·row_depth`; the II=1 part fits single-row points to **R²=1.0**.
  Exact.
- **`fill_const`** — one scalar, the residual command→bus latency.

The **only** fitted curve is **`row_depth(n_col)`** — the per-row ping-pong refill depth — recovered
from the inter-row gaps and carried as a saturating [`InterpCalibModel`](../../guide/calib/models.md)
lookup (not a `sqrt` basis fudge):

```python
db = CalibDataFrame.load("results/cosim_grid.json")
# row_depth measured from the inter-row gap: (y_write_span - trips) / (n_row - 1), per n_col
row_depth = InterpCalibModel(basis=["n_col"], target="row_depth").fit(gaps)   # the one curve
# occupancy + II=1 compute are deterministic; only fill_const is a scalar mean
```

Measured, it saturates — `row_depth(64) ≈ 70`, `(256) ≈ 260`, `(1024) ≈ 268` cycles — at the FIR
pipeline depth. The fitted model is committed in
[`results/fir_calibration.json`](../../../examples/rowwise_fir/results/fir_calibration.json), and the
full derivation in
[`results/fir_calibration_results.md`](../../../examples/rowwise_fir/results/fir_calibration_results.md).

## The result

The simulation loads the calibration and composes `whole = fill + max(write_occ, compute_body)`. Against
the RTL cosim it reproduces the whole-kernel to **≤ 1.3%** across the grid, with the honest
generalization tests well inside that:

| gate | point | sim | cosim | rel-err |
|---|---|---|---|---|
| interior held-out (`n_row=2` out; `row_depth(256)` trained) | (2, 256) | 1341.5 | 1343 | **0.11%** |
| untrained `n_col` (`row_depth` interpolated) | (4, 128) | 1211.3 | 1213 | **0.14%** |
| untrained `n_col` (`row_depth` interpolated) | (4, 512) | 3651.0 | 3673 | **0.60%** |

![Rowwise FIR — physical, near-fit-free cosim calibration. Panel (a): the model's predicted whole-kernel vs the RTL-measured whole-kernel across the 12-point grid plus two untrained-n_col held-out points, all on the y=x line (worst 1.3%). Panel (b): the one fitted curve, row_depth(n_col), three cosim samples and the interpolating lookup, saturating at ~268 cycles.](images/sim_vs_cosim.svg)

Panel (a) is the parity of predicted vs measured: every grid point — and the *untrained-column*
held-out points — lands on the diagonal. Panel (b) is the single fitted curve.

## What the residual says

The one fitted term, `row_depth(n_col)`, is a **per-row** quantity. At this *block* (per-matrix)
granularity the model can only see it summed as `n_row·row_depth` and must carry it as a measured
curve. A finer *per-row* (row-LT) model would treat it as each row's pipeline depth and obtain it
fit-free — so the residual is a single, physically-named, per-row quantity rather than diffuse
curvature. That is the quantified motivation for the next fidelity level.

## Reproducing the figure

The figure regenerates from the **committed JSONs alone** — no Vitis, no cosim — via
[`fir_figures.py`](../../../examples/rowwise_fir/fir_figures.py), deterministic (fixed `svg.hashsalt`),
with `--check` for a byte-match gate:

```bash
PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/fir_figures.py          # regenerate
PYTHONPATH=. ../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/fir_figures.py --check  # byte-identical?
```

## See also

- [The timing model](./timing_model.md) — the decomposition these terms fill in.
- [Calibration](../../guide/calib/) — the corpus and models (`row_depth` is an `InterpCalibModel`).
- [Fitting a timing model](../../guide/calib/fit.md) — the general fit this specializes.
